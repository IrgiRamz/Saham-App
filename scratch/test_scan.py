from app.services.screener import screener_service

signals = screener_service.run_screening()

# Silent mode: IHSG bearish / data tidak terverifikasi (fail-closed)
if signals and len(signals) == 1 and signals[0].get("is_market_bearish"):
    print("\n" + "=" * 50)
    print("SILENT MODE: Market bearish/berisiko - screening dibatalkan.")
    print(f"Alasan: {signals[0].get('reason')}")
    print("=" * 50)
else:
    print("\n" + "=" * 50)
    print(f"RESULTS: Found {len(signals)} top candidates today!")
    print("=" * 50)

    for idx, sig in enumerate(signals, 1):
        print(
            f"#{idx} | {sig['ticker']} | Score: {sig['score']}/100 | "
            f"Close: Rp {sig['close']:,.0f} | RSI: {sig['rsi14']:.2f} | "
            f"Turnover: Rp {sig['turnover']:,.0f}"
        )
