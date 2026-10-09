import asyncio, json, math, os
from datetime import date
import yfinance as yf
from analyse import analyser, backtest_texte
from binance.client import Client
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

VERSION = "v3 (analyse + backtest + scan + demo)"
TOKEN = os.environ["TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))  # votre ID Telegram
DUREE_MIN = 5            # durée de la position / de l'option simulée
MONTANT_USDT = 20        # montant par trade (argent FICTIF sur le testnet)
PERTE_MAX_JOUR = 50      # le bot s'arrête si la perte du jour dépasse ça (USDT)
STATS_FILE, TRADES_FILE = "stats.json", "trades.json"
CAPITAL = 1000           # capital de référence pour la taille de position
RISQUE_PCT = 1.0         # % du capital risqué par trade
ARRET = False            # interrupteur d'urgence

ALIAS = {
    "sp500": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI", "cac40": "^FCHI", "dax": "^GDAXI",
    "or": "GC=F", "argent": "SI=F", "petrole": "CL=F", "gaz": "NG=F",
}
DEVISES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "SEK", "NOK", "DKK", "PLN",
           "CZK", "HUF", "TRY", "ZAR", "MXN", "BRL", "CNH", "CNY", "HKD", "SGD", "INR", "KRW",
           "THB", "ILS", "MAD", "RUB", "AED", "SAR"}
CRYPTOS = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "LTC", "DOT", "AVAX", "LINK",
           "TRX", "SHIB", "ATOM", "XLM", "NEAR", "UNI", "PEPE", "TON", "BCH"}
FOREX = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD", "EURGBP",
         "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD", "GBPJPY", "GBPCHF", "GBPAUD",
         "GBPCAD", "GBPNZD", "AUDJPY", "AUDCAD", "AUDCHF", "AUDNZD", "CADJPY", "CADCHF",
         "CHFJPY", "NZDJPY", "NZDCAD", "NZDCHF"]
SCANS = {
    "forex": FOREX,
    "crypto": ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "LTC"],
    "indices": ["sp500", "nasdaq", "dow", "cac40", "dax"],
    "matieres": ["or", "argent", "petrole", "gaz"],
    "actions": ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META"],
}


def resoudre(nom):
    """Transforme un nom simple en ticker Yahoo Finance."""
    n = nom.lower().strip()
    if n in ALIAS:
        return ALIAS[n]
    u = n.upper()
    if len(u) == 6 and u[:3] in DEVISES and u[3:] in DEVISES:
        return f"{u}=X"          # paire forex
    if u.endswith("USDT"):
        return u[:-4] + "-USD"
    if u in CRYPTOS:
        return f"{u}-USD"        # crypto
    return u                     # action, indice ou autre ticker Yahoo


def charger(fichier, defaut):
    if os.path.exists(fichier):
        return json.load(open(fichier))
    return defaut


def sauver(fichier, data):
    json.dump(data, open(fichier, "w"))


def prix_et_signal(ticker):
    try:
        df = yf.download(ticker, period="1d", interval="1m", progress=False)
        close = df["Close"].squeeze().dropna()
    except Exception:
        return None, None
    if close.ndim != 1 or len(close) < 30:
        return None, None
    ema9 = close.ewm(span=9).mean().iloc[-1]
    ema21 = close.ewm(span=21).mean().iloc[-1]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    perte = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = float(100 - 100 / (1 + gain.iloc[-1] / perte.iloc[-1]))
    prix = float(close.iloc[-1])
    if ema9 > ema21 and rsi < 70:
        return prix, "HAUSSE"
    if ema9 < ema21 and rsi > 30:
        return prix, "BAISSE"
    return prix, "NEUTRE"


# ---------- Binance TESTNET (argent fictif) ----------
def binance():
    return Client(os.environ["BINANCE_KEY"], os.environ["BINANCE_SECRET"], testnet=True)


def acheter(symbol):
    o = binance().order_market_buy(symbol=symbol, quoteOrderQty=MONTANT_USDT)
    return float(o["executedQty"]), float(o["cummulativeQuoteQty"])


def vendre(symbol, qty):
    c = binance()
    base = symbol.replace("USDT", "")
    libre = float(c.get_asset_balance(asset=base)["free"])
    info = c.get_symbol_info(symbol)
    step = float(next(f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"))
    dec = max(0, round(-math.log10(step)))
    q = f"{math.floor(min(qty, libre) / step) * step:.{dec}f}"
    o = c.order_market_sell(symbol=symbol, quantity=q)
    return float(o["cummulativeQuoteQty"])


def pnl_jour():
    t = charger(TRADES_FILE, {})
    if t.get("date") != str(date.today()):
        t = {"date": str(date.today()), "pnl": 0.0, "nb": 0}
    return t


def autorise(update):
    return update.effective_user.id == OWNER_ID


# ---------- Commandes ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Bot de signaux + trading DÉMO (argent fictif).\nVersion : {VERSION}\n\n"
        "/actifs : liste des actifs\n"
        "/analyse eurusd : analyse pro + plan de trade\n"
        "/backtest eurusd : test de la stratégie sur 2 ans\n"
        "/signal eurusd : signal rapide\n"
        "/scan forex : analyse un groupe d'actifs\n"
        "/trade btc : ordre démo (cryptos)\n"
        "/solde : solde du compte démo\n"
        "/stats : réussite des signaux\n"
        "/stop : arrêt d'urgence\n"
        "/reprendre : relancer\n"
        "/monid : votre ID Telegram"
    )


async def monid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Votre ID Telegram : {update.effective_user.id}")


async def actifs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Vous pouvez analyser presque tout :\n"
        "- Forex : /signal eurusd, /signal usdjpy (toute paire de devises)\n"
        "- Crypto : /signal btc, xrp, doge...\n"
        "- Actions : /signal AAPL, MSFT, TSLA...\n"
        "- Indices : sp500, nasdaq, dow, cac40, dax\n"
        "- Matières : or, argent, petrole, gaz\n\n"
        "/scan forex (ou crypto, indices, matieres, actions) : analyse tout un groupe."
    )


async def signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nom = ctx.args[0].lower() if ctx.args else "btc"
    ticker = resoudre(nom)
    prix, sig = await asyncio.to_thread(prix_et_signal, ticker)
    if prix is None:
        await update.message.reply_text("Aucune donnée (nom incorrect ou marché fermé). Tapez /actifs")
        return
    if sig == "NEUTRE":
        await update.message.reply_text(f"{nom.upper()} : pas de signal clair. Ne rien faire.")
        return
    await update.message.reply_text(
        f"{nom.upper()} à {prix:.4f}\nSignal : {sig} pour {DUREE_MIN} min.\n"
        "Résultat dans quelques minutes."
    )
    ctx.job_queue.run_once(
        verifier, DUREE_MIN * 60,
        data={"ticker": ticker, "nom": nom, "prix": prix, "sig": sig},
        chat_id=update.effective_chat.id,
    )


async def cmd_analyse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nom = ctx.args[0] if ctx.args else "eurusd"
    await update.message.reply_text("Analyse en cours (tendance de fond + court terme)...")
    txt = await asyncio.to_thread(analyser, resoudre(nom), CAPITAL, RISQUE_PCT)
    await update.message.reply_text(txt or "Données insuffisantes (nom incorrect ou marché fermé).")


async def cmd_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nom = ctx.args[0] if ctx.args else "eurusd"
    await update.message.reply_text("Backtest en cours, ça prend un peu de temps...")
    txt = await asyncio.to_thread(backtest_texte, resoudre(nom))
    await update.message.reply_text(txt or "Données insuffisantes pour ce backtest.")


async def scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    groupe = ctx.args[0].lower() if ctx.args else "forex"
    if groupe not in SCANS:
        await update.message.reply_text("Groupes : " + ", ".join(SCANS))
        return
    await update.message.reply_text(f"Analyse de {len(SCANS[groupe])} actifs, patientez...")
    hausse, baisse = [], []
    for n in SCANS[groupe]:
        prix, sig = await asyncio.to_thread(prix_et_signal, resoudre(n))
        if sig == "HAUSSE":
            hausse.append(n.upper())
        elif sig == "BAISSE":
            baisse.append(n.upper())
    await update.message.reply_text(
        "HAUSSE : " + (", ".join(hausse) or "aucun") + "\n"
        "BAISSE : " + (", ".join(baisse) or "aucun") + "\n\n"
        "Tapez /signal NOM pour suivre un actif."
    )


async def verifier(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    df = yf.download(d["ticker"], period="1d", interval="1m", progress=False)
    nouveau = float(df["Close"].squeeze().dropna().iloc[-1])
    monte = nouveau > d["prix"]
    ok = (d["sig"] == "HAUSSE" and monte) or (d["sig"] == "BAISSE" and not monte)
    s = charger(STATS_FILE, {"gagne": 0, "perdu": 0})
    s["gagne" if ok else "perdu"] += 1
    sauver(STATS_FILE, s)
    await ctx.bot.send_message(
        ctx.job.chat_id,
        f"{d['nom'].upper()} : {d['prix']:.4f} → {nouveau:.4f}\n"
        f"Signal {d['sig']} : {'GAGNÉ ✅' if ok else 'PERDU ❌'}",
    )


async def trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        await update.message.reply_text("Accès refusé.")
        return
    if ARRET:
        await update.message.reply_text("Bot en arrêt d'urgence. Tapez /reprendre.")
        return
    nom = ctx.args[0].lower() if ctx.args else "btc"
    ticker = resoudre(nom)
    if not ticker.endswith("-USD"):
        await update.message.reply_text("Le trading démo (Binance) ne marche que sur les cryptos. Pour le reste, utilisez /signal.")
        return
    t = pnl_jour()
    if t["pnl"] <= -PERTE_MAX_JOUR:
        await update.message.reply_text(f"Perte max du jour atteinte ({t['pnl']:.2f} USDT). Stop jusqu'à demain.")
        return
    prix, sig = await asyncio.to_thread(prix_et_signal, ticker)
    if prix is None:
        await update.message.reply_text("Pas assez de données.")
        return
    if sig != "HAUSSE":
        await update.message.reply_text(f"{nom.upper()} : signal {sig}. Pas d'achat (on n'achète que sur HAUSSE).")
        return
    symbol = ticker[:-4] + "USDT"
    try:
        qty, cout = await asyncio.to_thread(acheter, symbol)
    except Exception as e:
        await update.message.reply_text(f"Erreur d'achat : {e}")
        return
    await update.message.reply_text(
        f"ACHAT DÉMO {nom.upper()} : {cout:.2f} USDT\nVente automatique dans {DUREE_MIN} min."
    )
    ctx.job_queue.run_once(
        cloturer, DUREE_MIN * 60,
        data={"symbol": symbol, "qty": qty, "cout": cout, "nom": nom},
        chat_id=update.effective_chat.id,
    )


async def cloturer(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    try:
        recu = await asyncio.to_thread(vendre, d["symbol"], d["qty"])
    except Exception as e:
        await ctx.bot.send_message(ctx.job.chat_id, f"Erreur de vente : {e}")
        return
    gain = recu - d["cout"]
    t = pnl_jour()
    t["pnl"] += gain
    t["nb"] += 1
    sauver(TRADES_FILE, t)
    await ctx.bot.send_message(
        ctx.job.chat_id,
        f"VENTE DÉMO {d['nom'].upper()} : {gain:+.2f} USDT\n"
        f"Total du jour : {t['pnl']:+.2f} USDT ({t['nb']} trades)",
    )


async def solde(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        await update.message.reply_text("Accès refusé.")
        return
    try:
        compte = await asyncio.to_thread(lambda: binance().get_account())
    except Exception as e:
        await update.message.reply_text(f"Erreur : {e}")
        return
    lignes = [f"{b['asset']} : {float(b['free']):.4f}" for b in compte["balances"] if float(b["free"]) > 0]
    await update.message.reply_text("Solde démo :\n" + "\n".join(lignes[:15]))


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = charger(STATS_FILE, {"gagne": 0, "perdu": 0})
    total = s["gagne"] + s["perdu"]
    taux = 100 * s["gagne"] / total if total else 0
    t = pnl_jour()
    await update.message.reply_text(
        f"Signaux : {total}\nGagnés : {s['gagne']}\nPerdus : {s['perdu']}\n"
        f"Taux de réussite : {taux:.1f} %\n"
        f"Trades démo aujourd'hui : {t['nb']} ({t['pnl']:+.2f} USDT)\n\n"
        "Rappel : il faut des centaines de signaux pour juger."
    )


async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ARRET
    if autorise(update):
        ARRET = True
        await update.message.reply_text("ARRÊT D'URGENCE activé. Plus d'ordres.")


async def reprendre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ARRET
    if autorise(update):
        ARRET = False
        await update.message.reply_text("Trading démo réactivé.")


async def erreur(update, ctx: ContextTypes.DEFAULT_TYPE):
    print(f"Erreur : {ctx.error}", flush=True)


app = ApplicationBuilder().token(TOKEN).build()
app.add_error_handler(erreur)
for nom, fn in [("start", start), ("monid", monid), ("actifs", actifs), ("signal", signal), ("scan", scan), ("analyse", cmd_analyse), ("backtest", cmd_backtest),
                ("trade", trade), ("solde", solde), ("stats", stats),
                ("stop", stop), ("reprendre", reprendre)]:
    app.add_handler(CommandHandler(nom, fn))
print(f"Bot démarré - version {VERSION}", flush=True)
app.run_polling()
