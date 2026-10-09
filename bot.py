import json, os
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.environ["TOKEN"]   # le token est mis dans Railway, pas dans le code
DUREE_MIN = 5           # durée de l'option simulée (minutes)
STATS_FILE = "stats.json"

ACTIFS = {
    "btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD",
    "eurusd": "EURUSD=X", "gbpusd": "GBPUSD=X", "usdjpy": "USDJPY=X",
    "or": "GC=F", "petrole": "CL=F",
    "sp500": "^GSPC", "nasdaq": "^IXIC",
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA",
}


def charger_stats():
    if os.path.exists(STATS_FILE):
        return json.load(open(STATS_FILE))
    return {"gagne": 0, "perdu": 0}


def sauver_stats(s):
    json.dump(s, open(STATS_FILE, "w"))


def prix_et_signal(ticker):
    df = yf.download(ticker, period="1d", interval="1m", progress=False)
    close = df["Close"].squeeze().dropna()
    if len(close) < 30:
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


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot de signaux (SIMULATION, aucun argent réel).\n\n"
        "/actifs : liste des actifs\n"
        "/signal btc : signal sur un actif\n"
        "/stats : taux de réussite réel"
    )


async def actifs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Actifs : " + ", ".join(ACTIFS))


async def signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nom = ctx.args[0].lower() if ctx.args else "btc"
    if nom not in ACTIFS:
        await update.message.reply_text("Actif inconnu. Tapez /actifs")
        return
    prix, sig = prix_et_signal(ACTIFS[nom])
    if prix is None:
        await update.message.reply_text("Pas assez de données (marché fermé ?).")
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
        data={"ticker": ACTIFS[nom], "nom": nom, "prix": prix, "sig": sig},
        chat_id=update.effective_chat.id,
    )


async def verifier(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    df = yf.download(d["ticker"], period="1d", interval="1m", progress=False)
    nouveau = float(df["Close"].squeeze().dropna().iloc[-1])
    monte = nouveau > d["prix"]
    ok = (d["sig"] == "HAUSSE" and monte) or (d["sig"] == "BAISSE" and not monte)
    s = charger_stats()
    s["gagne" if ok else "perdu"] += 1
    sauver_stats(s)
    await ctx.bot.send_message(
        ctx.job.chat_id,
        f"{d['nom'].upper()} : {d['prix']:.4f} → {nouveau:.4f}\n"
        f"Signal {d['sig']} : {'GAGNÉ ✅' if ok else 'PERDU ❌'}",
    )


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = charger_stats()
    total = s["gagne"] + s["perdu"]
    taux = 100 * s["gagne"] / total if total else 0
    await update.message.reply_text(
        f"Signaux : {total}\nGagnés : {s['gagne']}\nPerdus : {s['perdu']}\n"
        f"Taux de réussite : {taux:.1f} %\n\n"
        "Rappel : il faut > 55 % sur des centaines de signaux pour être rentable."
    )


app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("actifs", actifs))
app.add_handler(CommandHandler("signal", signal))
app.add_handler(CommandHandler("stats", stats))
app.run_polling()
