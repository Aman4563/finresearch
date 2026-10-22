STREAM: price behaviour of {company_name} ({nse_symbol}).

Use nse_price_history for at least the last 12 months (and 3 years if useful) and fincalc.market:
- returns over 1m / 3m / 12m, annualised volatility, max drawdown and when it happened;
- position in the 52-week range, 50- and 200-day moving averages and whether price is above them;
- volume trend and delivery behaviour where available; dividend and corporate-action effects (ex-dates).
This is context for timing, not a trading signal. Cite each figure to the `source` URL the tool returns (the exact
NSE or BSE request for this stock) with the access time.
Option-implied context (optional): option_implied_move, option_iv30_percentile and option_skew_25d read NSE's stored
end-of-day F&O data for NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY and {nse_symbol} only. Cite a figure with the `citation`
the tool returns, setting result_key to the key you quote; state the trade date and expiry with it. When a tool
answers known=false, say the figure is unknown and why; never estimate it. An implied move is what option prices
imply, not a forecast.
Focus: {focus}
