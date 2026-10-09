import numpy as np
import pandas as pd
import yfinance as yf

COUT_R = 0.1  # frais/spread estimés par trade, en multiple du risque


def telecharger(ticker, interval, period):
    try:
        df = yf.download(ticker, interval=interval, period=period,
                         progress=False, auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def indicateurs(df):
    c, h, l = df["Close"], df["High"], df["Low"]
    d = pd.DataFrame(index=df.index)
    d["open"], d["high"], d["low"], d["close"] = df["Open"], h, l, c
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()
    d["ema200"] = c.ewm(span=200, adjust=False).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    perte = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + gain / perte.replace(0, np.nan))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    d["macd_h"] = macd - macd.ewm(span=9, adjust=False).mean()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    up, dn = h.diff(), -l.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    pdi = 100 * plus.ewm(alpha=1 / 14, adjust=False).mean() / d["atr"]
    mdi = 100 * minus.ewm(alpha=1 / 14, adjust=False).mean() / d["atr"]
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    d["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()
    return d


def score(d):
    """Score de -4 (très baissier) à +4 (très haussier)."""
    s = np.where(d["ema20"] > d["ema50"], 1, -1)
    s = s + np.where(d["close"] > d["ema200"], 1, -1)
    s = s + np.where(d["macd_h"] > 0, 1, -1)
    s = s + np.where(d["rsi"] > 55, 1, np.where(d["rsi"] < 45, -1, 0))
    return pd.Series(s, index=d.index)


def signaux(d):
    """+1 achat, -1 vente, 0 rien. Filtre : seulement si le marché a une tendance (ADX > 20)."""
    s = score(d)
    sig = np.where((s >= 3) & (d["adx"] > 20), 1, np.where((s <= -3) & (d["adx"] > 20), -1, 0))
    return pd.Series(sig, index=d.index)


def analyser(ticker, capital=1000.0, risque_pct=1.0):
    h1 = telecharger(ticker, "1h", "120d")
    j1 = telecharger(ticker, "1d", "2y")
    if h1 is None or j1 is None or len(h1) < 210 or len(j1) < 210:
        return None
    d, dj = indicateurs(h1), indicateurs(j1)
    s = int(score(d).iloc[-1])
    adx = float(d["adx"].iloc[-1])
    rsi = float(d["rsi"].iloc[-1])
    prix = float(d["close"].iloc[-1])
    atr = float(d["atr"].iloc[-1])
    fond = 1 if dj["ema50"].iloc[-1] > dj["ema200"].iloc[-1] else -1

    if adx < 20:
        verdict = f"ATTENDRE : marché sans tendance (ADX {adx:.0f})"
    elif s >= 3 and fond == 1:
        verdict = "ACHAT"
    elif s <= -3 and fond == -1:
        verdict = "VENTE"
    elif abs(s) >= 3:
        verdict = "ATTENDRE : signal contre la tendance de fond"
    else:
        verdict = "ATTENDRE : signaux mitigés"

    txt = (f"Prix : {prix:.5g}\n"
           f"Tendance de fond (jour) : {'haussière' if fond == 1 else 'baissière'}\n"
           f"Score court terme : {s:+d}/4 | RSI {rsi:.0f} | ADX {adx:.0f}\n"
           f"Verdict : {verdict}")

    if verdict in ("ACHAT", "VENTE"):
        sens = 1 if verdict == "ACHAT" else -1
        dist = 1.5 * atr
        stop = prix - sens * dist
        cible = prix + sens * dist * 2
        risque_monnaie = capital * risque_pct / 100
        taille = risque_monnaie / dist if dist > 0 else 0
        txt += (f"\n\nPlan de trade :\nEntrée : {prix:.5g}\nStop : {stop:.5g}\n"
                f"Objectif : {cible:.5g} (gain = 2x le risque)\n"
                f"Taille : {taille:.4g} unités (risque {risque_pct:g} % = {risque_monnaie:.2f})")
    return txt


def simuler(d, rr=2.0, mult_stop=1.5, max_bars=24):
    sg = signaux(d).values
    o, h, l, c, atr = (d[k].values for k in ("open", "high", "low", "close", "atr"))
    n, res, i = len(d), [], 200
    while i < n - 2:
        if sg[i] != 0 and sg[i] != sg[i - 1] and not np.isnan(atr[i]) and atr[i] > 0:
            sens = sg[i]
            entree = o[i + 1]
            risque = mult_stop * atr[i]
            stop = entree - sens * risque
            cible = entree + sens * risque * rr
            sortie = min(i + 1 + max_bars, n - 1)
            r, fin = None, sortie
            for j in range(i + 1, sortie + 1):
                stop_touche = l[j] <= stop if sens == 1 else h[j] >= stop
                cible_touchee = h[j] >= cible if sens == 1 else l[j] <= cible
                if stop_touche:            # prudent : le stop passe avant l'objectif
                    r, fin = -1.0, j
                    break
                if cible_touchee:
                    r, fin = rr, j
                    break
            if r is None:
                r = sens * (c[sortie] - entree) / risque
            res.append(r - COUT_R)
            i = fin + 1
        else:
            i += 1
    return res


def stats_backtest(res):
    if len(res) == 0:
        return None
    r = np.array(res)
    gains, pertes = r[r > 0].sum(), -r[r < 0].sum()
    cumul = np.cumsum(r)
    creux = float((np.maximum.accumulate(cumul) - cumul).max())
    return {
        "n": len(r), "win": 100 * float((r > 0).mean()), "esp": float(r.mean()),
        "pf": float(gains / pertes) if pertes > 0 else float("inf"),
        "total": float(cumul[-1]), "dd": creux,
    }


def backtest_texte(ticker):
    df = telecharger(ticker, "1h", "730d")
    if df is None or len(df) < 400:
        return None
    st = stats_backtest(simuler(indicateurs(df)))
    if st is None:
        return "Aucun trade généré sur la période."
    if st["n"] < 30:
        avis = "Pas assez de trades pour conclure."
    elif st["esp"] > 0.1 and st["pf"] > 1.2:
        avis = "Résultat encourageant, à confirmer en démo avant tout argent réel."
    else:
        avis = "Aucun avantage démontré : ne tradez pas cette stratégie avec de l'argent réel."
    return (f"Backtest ~2 ans (bougies 1h, frais inclus)\n"
            f"Trades : {st['n']}\nTaux de réussite : {st['win']:.0f} %\n"
            f"Gain moyen par trade : {st['esp']:+.2f} R\n"
            f"Profit factor : {st['pf']:.2f}\n"
            f"Résultat total : {st['total']:+.1f} R\n"
            f"Pire baisse : {st['dd']:.1f} R\n\n{avis}\n"
            "(1 R = le montant risqué par trade. Le passé ne garantit pas l'avenir.)")


def signal_klines(k):
    """k = bougies Binance (liste). Retourne (signal -1/0/+1, ATR en % du prix) sur la dernière bougie clôturée."""
    df = pd.DataFrame(k).iloc[:, 1:5].astype(float)
    df.columns = ["Open", "High", "Low", "Close"]
    if len(df) < 210:
        return 0, 0.0
    d = indicateurs(df)
    sg = int(signaux(d).iloc[-2])
    atr_pct = float(d["atr"].iloc[-2] / d["close"].iloc[-2])
    return sg, atr_pct
