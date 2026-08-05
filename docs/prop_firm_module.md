\# Prop Firm Evaluation Module



\# Quant Research Terminal



\## Purpose



The Prop Firm Evaluation Module estimates the probability of successfully passing proprietary trading evaluations and surviving after funding.



The module is intended for quantitative research rather than discretionary trading.



It should support multiple prop firms and configurable evaluation rules.



\---



\# Objectives



The module should answer questions such as:



\- What is the probability of passing this evaluation?

\- How many attempts are expected?

\- What is the probability of reaching a funded account?

\- What is the probability of surviving 30, 60, 90 and 180 trading days?

\- Which risk model maximizes long-term survival?



\---



\# Supported Evaluation Types



Initially:



\- One-phase evaluations

\- Two-phase evaluations



Future:



\- Three-phase evaluations

\- Instant funding

\- Custom evaluation templates



\---



\# Configurable Rules



Every prop firm must define:



\- Profit target

\- Daily drawdown

\- Maximum drawdown

\- Trailing drawdown

\- Trading days requirement

\- Consistency rule

\- Maximum leverage

\- News restrictions

\- Overnight holding rules

\- Weekend holding rules



No rules should be hardcoded.



\---



\# Monte Carlo Simulation



The module should support Monte Carlo simulation using historical strategy results.



Simulation inputs:



\- trade sequence

\- expectancy

\- win rate

\- payoff ratio

\- commissions

\- slippage



Outputs:



\- probability of passing

\- probability of failure

\- confidence intervals

\- distribution of attempts



\---



\# Funded Account Survival



The module should estimate survival probability after funding.



Metrics include:



\- probability of surviving 30 days

\- probability of surviving 60 days

\- probability of surviving 90 days

\- probability of surviving 180 days

\- expected account lifetime



\---



\# Risk Metrics



Supported metrics:



\- Maximum Drawdown

\- Daily Drawdown

\- Consecutive Losses

\- Consecutive Wins

\- Ulcer Index

\- Profit Factor

\- Expectancy

\- Sharpe Ratio

\- Sortino Ratio

\- Calmar Ratio



\---



\# Position Sizing Analysis



Support comparison of:



\- Fixed contracts

\- Fixed fractional risk

\- Fixed percentage risk

\- Kelly fraction

\- Fractional Kelly



The same strategy should be evaluated under multiple sizing models.



\---



\# Scenario Analysis



Evaluate multiple scenarios:



\- optimistic

\- expected

\- pessimistic



\---



\# Batch Evaluation



Support evaluation of:



\- multiple strategies

\- multiple prop firms

\- multiple sizing models



Generate comparison reports automatically.



\---



\# Reporting



Each report should include:



\- pass probability

\- funded probability

\- expected attempts

\- survival curves

\- drawdown distribution

\- equity distribution

\- trade distribution

\- Monte Carlo statistics



Reports should be exportable.



\---



\# Future Extensions



Future versions may include:



\- Bayesian updating

\- Regime-aware simulations

\- Correlated strategy portfolios

\- Multi-account optimization

\- Capital allocation optimization



\---



\# Acceptance Criteria



The module is considered complete only when it is:



✓ deterministic



✓ reproducible



✓ configurable



✓ statistically validated



✓ fully tested



✓ documented

