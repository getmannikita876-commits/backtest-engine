\# Backtest Validation



\# Quant Research Terminal



\## Purpose



This document defines the validation framework used to determine whether a backtest result is trustworthy.



A profitable backtest is not automatically a valid strategy.



Every strategy must survive a rigorous validation process before it is considered suitable for further research or live trading.



\---



\# Validation Philosophy



The objective of validation is not to prove that a strategy works.



The objective is to actively search for reasons why the strategy might fail.



The burden of proof is always on the strategy.



\---



\# Required Validation Pipeline



Every strategy must pass all stages:



1\. Data Validation

2\. In-Sample Development

3\. Out-of-Sample Validation

4\. Walk-Forward Analysis

5\. Monte Carlo Simulation

6\. Parameter Sensitivity Analysis

7\. Robustness Testing

8\. Statistical Validation



Failure at any stage invalidates the strategy.



\---



\# Data Validation



Before testing begins verify:



\- timestamps

\- duplicate records

\- missing values

\- timezone correctness

\- session boundaries

\- futures roll handling

\- option metadata integrity



No strategy should run on corrupted data.



\---



\# In-Sample



The in-sample period is used only for:



\- hypothesis testing

\- feature engineering

\- parameter development



Never evaluate final performance using only in-sample data.



\---



\# Out-of-Sample



Out-of-sample testing is mandatory.



The strategy must demonstrate similar behavior on unseen data.



Large performance degradation indicates overfitting.



\---



\# Walk-Forward Analysis



Walk-forward analysis should evaluate:



\- parameter stability

\- regime stability

\- consistency across time



Parameter re-optimization must follow predefined rules.



\---



\# Parameter Sensitivity



Small parameter changes should not dramatically alter performance.



Strategies that only work for one parameter combination are considered unstable.



\---



\# Monte Carlo Simulation



Monte Carlo analysis should estimate:



\- return distribution

\- drawdown distribution

\- probability of ruin

\- confidence intervals

\- expected variability



\---



\# Robustness Tests



Evaluate strategy behavior under:



\- increased commissions

\- increased slippage

\- execution delays

\- missing trades

\- market regime changes

\- volatility shifts



A robust strategy should tolerate moderate disturbances.



\---



\# Performance Metrics



Minimum reporting should include:



\- Net Profit

\- CAGR

\- Sharpe Ratio

\- Sortino Ratio

\- Calmar Ratio

\- Profit Factor

\- Expectancy

\- Win Rate

\- Average Trade

\- Maximum Drawdown

\- Recovery Factor

\- Ulcer Index



\---



\# Failure Conditions



Reject the strategy if:



\- out-of-sample performance collapses

\- profitability depends on one parameter

\- realistic costs eliminate profitability

\- drawdowns become unacceptable

\- results cannot be reproduced



\---



\# Documentation



Every validation report should include:



\- hypothesis

\- methodology

\- datasets

\- parameters

\- validation results

\- limitations

\- conclusions



\---



\# Acceptance Criteria



A strategy should only be considered valid when it is:



✓ reproducible



✓ statistically significant



✓ economically plausible



✓ robust



✓ profitable after realistic execution costs



✓ validated on unseen data



No strategy is accepted based solely on historical profit.

