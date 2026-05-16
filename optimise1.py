import random
import time
import pandas as pd
from hardened_backtest import fetch_bybit_data, compute_all_indicators, generate_signals, execute_backtest, INITIAL_BALANCE, SYMBOL, INTERVAL, DURATION

def optimize():
    print("Pre-computing Mean Reversion Indicators ONCE...")
    df_raw = fetch_bybit_data(SYMBOL, INTERVAL, DURATION)
    df_base = compute_all_indicators(df_raw)
    
    top_results = []
    iterations = 0
    start_time = time.time()
    
    print("\nStarting infinite Mean Reversion Optimization loop...")
    print("Target: >= 2.0% Avg Monthly Return AND <= 12.0% Max Drawdown\n")
    
    while True:
        iterations += 1
        
        rsi_buy = random.randint(15, 45)
        rsi_sell = random.randint(55, 85)
        sl_mult = round(random.uniform(1.5, 6.0), 2)
        risk_pct = round(random.uniform(0.01, 0.05), 4)
        
        if rsi_buy >= rsi_sell: continue
            
        df_sig = generate_signals(df_base, rsi_buy, rsi_sell)
        trades, bal_dict = execute_backtest(df_sig, INITIAL_BALANCE, risk_pct, sl_mult)
        if not trades: continue
            
        trade_df = pd.DataFrame(trades)
        bal_series = pd.Series(list(bal_dict.values()), index=pd.to_datetime(list(bal_dict.keys())))
        
        total_ret = (bal_series.iloc[-1] - INITIAL_BALANCE) / INITIAL_BALANCE * 100
        days = (bal_series.index[-1] - bal_series.index[0]).days
        months = days / 30.44 if days > 0 else 1
        avg_monthly = total_ret / months
        dd = ((bal_series - bal_series.cummax()) / bal_series.cummax() * 100).min()
        win_rate = len(trade_df[trade_df["is_win"]]) / len(trade_df) * 100
        
        result = {
            "monthly": round(avg_monthly, 2),
            "dd": round(dd, 2),
            "trades": len(trades),
            "wr": round(win_rate, 2),
            "params": f"RSI_Buy:{rsi_buy} RSI_Sell:{rsi_sell} SL_ATR:{sl_mult} Risk:{risk_pct*100:.2f}%"
        }
        
        top_results.append(result)
        top_results = sorted(top_results, key=lambda x: (x["monthly"], x["dd"]), reverse=True)[:5]
        
        if avg_monthly >= 2.0 and dd >= -12.0:
            print("\n🚀 TARGET REACHED! Found parameter set achieving goals!")
            print(f"Monthly: {avg_monthly:.2f}% | Max DD: {dd:.2f}% | Win Rate: {win_rate:.2f}%")
            print(f"Params: {result['params']}")
            with open("target_strategy_found.txt", "a") as f:
                f.write(f"Target Reached! Monthly: {avg_monthly:.2f}% | DD: {dd:.2f}%\n")
                f.write(f"Params: {result['params']}\n---\n")
                
        if iterations % 20 == 0:
            elapsed = time.time() - start_time
            print(f"Iter: {iterations} | Speed: {iterations/elapsed:.1f} iters/sec | Best Monthly: {top_results[0]['monthly']}% (DD: {top_results[0]['dd']}%)")
            for i, res in enumerate(top_results):
                print(f" {i+1}. Ret/mo: {res['monthly']:>5.2f}% | DD: {res['dd']:>6.2f}% | WR: {res['wr']:>4.1f}% | {res['params']}")
            print("-" * 90)

if __name__ == "__main__":
    optimize()
