[![make.fun bounty](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/m1651/makefun-pump-fun-replay-bot/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/m1651/makefun-pump-fun-replay-bot/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

# Pump.fun Replay Sprint — Paper Bot

A paper-money trading bot for the make.fun "Pump.fun Replay Sprint — Paper Bot, Real Ranking" bounty. It trades 10 SOL of paper money on a 24-hour replay of recorded pump.fun bonding-curve activity (agent-fun/replay/v2 protocol, Python 3 stdlib only, no network, fully deterministic).

## Strategy: tape surge

- **Watch the tape.** Every curve keeps a rolling tape of its recorded trades. A curve becomes a buy only once it is past its launch-dump window (400+ slots old), still small (1.5–70 SOL of real reserves), and its recent tape shows a genuine surge: at least 5 buy trades from at least 5 distinct buyers, net SOL flowing in, the price up 4–120% over the window and sitting within 4% of the window high, with inflow accelerating in the newer half of the window and no single buyer dominating.
- **Start small, pyramid into strength.** The first buy is 0.08 SOL; adds of 0.12 SOL follow only while the price keeps stepping up and the position is in profit, up to 0.5 SOL per coin. False starts cost little; real runners get size.
- **Exit whole, fast.** Stop loss 18% below cost, a trailing stop 25% off the peak value once armed at +8%, a fade exit when net flow turns negative, a big-sell exit, or a 1,000-slot maximum hold. Curves that complete are closed by the replay itself.
- **Risk limits.** At most 4 momentum positions plus 1 probe slot (5 open positions max), 5 SOL total exposure, and a drawdown circuit breaker that stops opening new positions once the portfolio has fallen 50% from the 10 SOL starting capital — inside the competition's 50% max-drawdown guardrail.
- **Guardrail pacing.** The run must end with at least 20 distinct coins traded. When fills or coins lag the pace set in block time, the bot opens tiny 0.01 SOL probe round trips on liquid curves and sells them as soon as the buy lands.

## Layout

- `main.py` — the bot (entrypoint; the grader runs `python3 -P -s main.py`)
- `vendor/makefun_bot.py` — the kit's Python protocol shim (copied from the kit's SDK)
- `LICENSE` — MIT

## Running locally

The kit's local grader scores the bot on the dev window:

```
node cli/makefun.mjs grade-local --kit <kit dir> --bot <this repo> --driver process
```
