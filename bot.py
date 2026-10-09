import asyncio, json, math, os, time
from datetime import date
import yfinance as yf
from analyse import analyser, backtest_texte, signal_klines
from binance.client import Client
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

VERSION = "v4 (auto-trading démo tous marchés Binance)"
TOKEN = os.environ["TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))  # votre ID Telegram
DUREE_MIN = 5            # durée de la position / de l'option simulée
MONTANT_USDT = 20        # montant par trade (argent FICTIF sur le testnet)
PERTE_MAX_JOUR = 50      # le bot s'arrête si la perte du jour dépasse ça (USDT)
STATS_FILE, TRADES_FILE = "stats.json", "trades.json"
CAPITAL = 1000           # capital de référence pour la taille de position
RISQUE_PCT = 1.0         # % du capital risqué par trade
ARRET = False            # interrupteur d'urgence
# --- Trading automatique démo sur les marchés Binance ---
AUTO_MIN = 15            # fréquence d'analyse (minutes)
NB_MARCHES = 40          # nombre de marchés analysés (les plus liquides)
MAX_POSITIONS = 3        # positions ouvertes en même temps
DUREE_MAX_H = 6          # fermeture automatique après X heures
STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "EUR", "AEUR", "USD1", "XUSD", "PYUSD"}

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
        "/binance : analyse tous les marchés Binance\n"
        "/auto on|off : trading auto démo sur les marchés Binance\n"
        "/positions : positions ouvertes\n"
        "/fermertout : vendre toutes les positions\n"
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


# ---------- Trading automatique (démo) ----------
def client_public():
    return Client()  # données de marché, sans clé


def univers():
    """Les marchés USDT les plus liquides de Binance, tradables sur le testnet."""
    tickers = client_public().get_ticker()
    interdits = ("UP", "DOWN", "BULL", "BEAR")
    cands = [t for t in tickers
             if t["symbol"].endswith("USDT")
             and t["symbol"][:-4] not in STABLES
             and not t["symbol"][:-4].endswith(interdits)]
    cands.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    info = binance().get_exchange_info()
    ok = {x["symbol"] for x in info["symbols"] if x["status"] == "TRADING"}
    return [t["symbol"] for t in cands if t["symbol"] in ok][:NB_MARCHES]


def scanner_binance():
    pub = client_public()
    res = []
    for sym in univers():
        try:
            k = pub.get_klines(symbol=sym, interval="1h", limit=300)
            sg, atr_pct = signal_klines(k)
            res.append((sym, sg, atr_pct))
        except Exception:
            continue
    return res


async def cmd_binance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        await update.message.reply_text("Accès refusé.")
        return
    await update.message.reply_text(f"Analyse des {NB_MARCHES} marchés Binance les plus liquides...")
    try:
        res = await asyncio.to_thread(scanner_binance)
    except Exception as e:
        await update.message.reply_text(f"Erreur : {e}")
        return
    h = [r[0].replace("USDT", "") for r in res if r[1] == 1]
    b = [r[0].replace("USDT", "") for r in res if r[1] == -1]
    await update.message.reply_text(
        f"Marchés analysés : {len(res)}\nHAUSSE : {', '.join(h) or 'aucun'}\n"
        f"BAISSE : {', '.join(b) or 'aucun'}\n\n/auto on pour trader automatiquement (démo)."
    )


async def fermer_position(bot, chat_id, pos, sym, raison):
    p = pos.pop(sym, None)
    if p is None:
        return
    try:
        recu = await asyncio.to_thread(vendre, sym, p["qty"])
    except Exception as e:
        pos[sym] = p
        await bot.send_message(chat_id, f"Erreur de vente {sym} : {e}")
        return
    gain = recu - p["cout"]
    t = pnl_jour()
    t["pnl"] += gain
    t["nb"] += 1
    sauver(TRADES_FILE, t)
    await bot.send_message(
        chat_id,
        f"VENTE DÉMO {sym.replace('USDT', '')} ({raison}) : {gain:+.2f} USDT\n"
        f"Total du jour : {t['pnl']:+.2f} USDT ({t['nb']} trades)",
    )


async def cycle_auto(ctx: ContextTypes.DEFAULT_TYPE):
    pos = ctx.application.bot_data.setdefault("positions", {})
    chat = ctx.job.chat_id
    if ARRET:
        return
    if pnl_jour()["pnl"] <= -PERTE_MAX_JOUR:
        return
    libres = MAX_POSITIONS - len(pos)
    if libres <= 0:
        return
    try:
        res = await asyncio.to_thread(scanner_binance)
    except Exception as e:
        await ctx.bot.send_message(chat, f"Erreur d'analyse : {e}")
        return
    for sym, sg, atr_pct in [r for r in res if r[1] == 1 and r[0] not in pos][:libres]:
        try:
            qty, cout = await asyncio.to_thread(acheter, sym)
        except Exception as e:
            await ctx.bot.send_message(chat, f"Erreur d'achat {sym} : {e}")
            continue
        pos[sym] = {"qty": qty, "cout": cout, "entree": cout / qty,
                    "stop_pct": 1.5 * atr_pct, "cible_pct": 3 * atr_pct, "ouvert": time.time()}
        await ctx.bot.send_message(
            chat,
            f"ACHAT DÉMO {sym.replace('USDT', '')} : {cout:.2f} USDT\n"
            f"Stop -{1.5 * atr_pct * 100:.1f} % | Objectif +{3 * atr_pct * 100:.1f} %",
        )


async def surveiller(ctx: ContextTypes.DEFAULT_TYPE):
    pos = ctx.application.bot_data.setdefault("positions", {})
    for sym, p in list(pos.items()):
        try:
            prix = float(await asyncio.to_thread(lambda s=sym: binance().get_symbol_ticker(symbol=s)["price"]))
        except Exception:
            continue
        var = prix / p["entree"] - 1
        age_h = (time.time() - p["ouvert"]) / 3600
        raison = None
        if var <= -p["stop_pct"]:
            raison = "STOP"
        elif var >= p["cible_pct"]:
            raison = "OBJECTIF"
        elif age_h >= DUREE_MAX_H:
            raison = "TEMPS"
        if raison:
            await fermer_position(ctx.bot, ctx.job.chat_id, pos, sym, raison)


def demarrer_auto(job_queue, chat_id):
    for nom in ("auto", "surveil"):
        for j in job_queue.get_jobs_by_name(nom):
            j.schedule_removal()
    job_queue.run_repeating(cycle_auto, interval=AUTO_MIN * 60, first=10, chat_id=chat_id, name="auto")
    job_queue.run_repeating(surveiller, interval=60, first=30, chat_id=chat_id, name="surveil")


async def cmd_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        await update.message.reply_text("Accès refusé.")
        return
    arg = ctx.args[0].lower() if ctx.args else "statut"
    if arg == "on":
        demarrer_auto(ctx.job_queue, update.effective_chat.id)
        await update.message.reply_text(
            f"Trading automatique DÉMO activé.\nAnalyse de {NB_MARCHES} marchés toutes les {AUTO_MIN} min, "
            f"{MAX_POSITIONS} positions max, {MONTANT_USDT} USDT par trade.\n/auto off pour arrêter, /stop en urgence."
        )
    elif arg == "off":
        for j in ctx.job_queue.get_jobs_by_name("auto"):
            j.schedule_removal()
        await update.message.reply_text("Trading automatique arrêté : plus de nouveaux achats. Les positions ouvertes restent surveillées (stop et objectif) ; /fermertout pour les vendre.")
    else:
        actif = bool(ctx.job_queue.get_jobs_by_name("auto"))
        await update.message.reply_text(f"Trading automatique : {'ACTIF' if actif else 'arrêté'}\n" + texte_positions(ctx))


def texte_positions(ctx):
    pos = ctx.application.bot_data.get("positions", {})
    if not pos:
        return "Aucune position ouverte."
    lignes = [f"{s.replace('USDT', '')} : {p['cout']:.2f} USDT, ouverte depuis {(time.time() - p['ouvert']) / 60:.0f} min"
              for s, p in pos.items()]
    return "Positions ouvertes :\n" + "\n".join(lignes)


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if autorise(update):
        await update.message.reply_text(texte_positions(ctx))


async def fermertout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    pos = ctx.application.bot_data.setdefault("positions", {})
    for sym in list(pos):
        await fermer_position(ctx.bot, update.effective_chat.id, pos, sym, "MANUEL")
    await update.message.reply_text("Toutes les positions ont été fermées.")


async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ARRET
    if autorise(update):
        ARRET = True
        await update.message.reply_text("ARRÊT D'URGENCE : plus aucune nouvelle entrée. Les positions ouvertes restent surveillées (/fermertout pour tout vendre).")


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
                ("binance", cmd_binance), ("auto", cmd_auto), ("positions", cmd_positions),
                ("fermertout", fermertout), ("stop", stop), ("reprendre", reprendre)]:
    app.add_handler(CommandHandler(nom, fn))
if os.environ.get("AUTO") == "1" and OWNER_ID:
    demarrer_auto(app.job_queue, OWNER_ID)
print(f"Bot démarré - version {VERSION}", flush=True)
app.run_polling()
