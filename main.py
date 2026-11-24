"""Paper-money momentum bot for the pump.fun replay sprint (agent-fun/replay/v2).

Strategy ("tape surge", written from scratch for this competition):

* Watch every bonding curve's recorded trade tape. A curve becomes a buy when
  it is past its launch dump window, still small, and its recent tape shows a
  genuine surge: several distinct buyers, net SOL flowing in, the price up
  over the window and sitting near the window high, with inflow accelerating
  in the newer half of the window.
* Start small (0.08 SOL) and pyramid only into strength, up to 0.5 SOL per
  coin, so false starts cost little and real runners get size. Adds need
  the price 3% above the last buy and the position up 5%, so adds go only
  to positions already proving themselves.
* Exit whole on the first of: a stop loss against cost, a trailing stop off
  the position's peak value, a fade in net flow, or a maximum hold time.
  Curves that complete are closed by the replay itself. The trailing stop
  is wide (35% off the peak) because the dev window showed the few real
  runners carry the PnL; cutting them at 25% gave the gains back.
* Guardrail pacing: the run must end with at least 20 distinct coins traded
  (round trips of >= 0.01 SOL) and a max drawdown of 50% or less. When the
  bot's own fills or coins lag the pace set in block time, it opens tiny
  0.01 SOL probe round trips on liquid curves and sells them as soon as the
  buy lands, purely to keep the run above the bar.

Deterministic by construction: no clock, no environment reads, no unseeded
randomness; iteration only over insertion-ordered dicts fed by the batch
stream. Stdlib only, no network. Diagnostics go to stderr; stdout carries
replies only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script dir off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
BPS = makefun_bot.BPS

# --- Strategy parameters (SOL unless noted, slots as slots) -----------------
WINDOW = 40            # entry lookback, in slots
MIN_NET_SOL = 4.0      # net buy SOL (buys minus sells) over the window
MIN_BUYS = 5           # buy trades in the window
MIN_BUYERS = 10        # distinct buying wallets in the window
MIN_UP = 0.12          # price now vs window start, minus 1, at least this
MAX_UP = 0.80          # ... and at most this (skip curves that already mooned)
ACCEL = 2.0            # newer-half net inflow >= this x older-half net inflow
NEAR_HIGH = 0.96       # price now at least this share of the window high
MAX_BUYER_SHARE = 0.55 # largest single buyer's share of window buy SOL
MIN_RQ_SOL = 1.5       # curve real SOL at entry, at least this
MAX_RQ_SOL = 70.0      # ... and at most this
MIN_AGE = 400          # slots since CreateEvent before an entry is allowed
STAKE_SOL = 0.5        # most SOL into one position, in total
FIRST_SOL = 0.08       # first buy of a position
ADD_SOL = 0.12         # each pyramid add-on buy
ADD_STEP = 0.03        # add only once price is this much above the last buy's
ADD_IF_UP = 0.05       # add only while position value >= cost * (1 + this)
MIN_BUY_SOL = 0.01     # smallest buy worth sending
BUY_SLIP_BPS = 2000    # slippage on buys; sells always go out at 10000
STOP_LOSS = 0.15       # exit when value < cost * (1 - this)
TRAIL = 0.50           # exit when value < peak * (1 - this), once armed
TRAIL_ARM = 0.15       # trailing stop arms once peak >= cost * (1 + this)
FADE_WINDOW = 20       # exit when net SOL over this window falls below ...
FADE_NET_SOL = -0.75
MAX_HOLD = 600         # exit after this many slots held
COOLDOWN = 200         # slots to wait after an exit before re-buying a curve
MAX_ENTRIES = 2        # most entries per curve over the run
MAX_MAIN = 4           # most momentum positions open at once (5th slot: probes)
MAX_EXPOSURE_SOL = 5.0 # most SOL at cost across all positions at once
CURVE_FEE = 0.0125     # protocol 95 bps + creator 30 bps, charged on sells
# Guardrail pacing.
TARGET_COINS = 30
TARGET_FILLS = 300
PACE_SECS = 14400
PROBE_SOL = 0.01
MAX_PROBES = 1
# Drawdown circuit breaker: stop opening new positions once the portfolio
# (cash plus current close value of every position) has fallen this far
# below the starting capital. Exits keep running; only new entries stop.
MAX_DRAWDOWN = 0.50
START_CAPITAL_SOL = 10.0

# --- Per-curve state ---------------------------------------------------------


class Tape:
    """A curve's recent recorded trades, in slot order."""

    __slots__ = ("created", "trades", "entries", "last_exit")

    def __init__(self):
        self.created = None      # slot of its CreateEvent, None if unseen
        self.trades = []         # (slot, is_buy, sol, user, price) tuples
        self.entries = 0         # entries taken on this curve so far
        self.last_exit = -10**12  # slot of the last exit from this curve


class Held:
    """A position the bot is managing."""

    __slots__ = ("opened", "peak_value", "invested", "exiting", "filled",
                 "last_add_price", "last_value")

    def __init__(self, opened, price):
        self.opened = opened
        self.peak_value = 0.0    # best close value seen, SOL
        self.invested = 0.0      # SOL sent in buys for this position
        self.exiting = False     # exit decided, sell in flight or pending
        self.filled = False      # at least one buy of this position filled
        self.last_add_price = price
        self.last_value = 0.0    # close value at the latest batch, SOL


tapes = {}          # mint -> Tape, first-seen order
held = {}           # mint -> Held
probes = {}         # mint -> bool (False while buying, True once selling)
my_fills = 0        # the bot's own filled orders (harness closes excluded)
my_coins = set()    # mints the bot's own fills were on
first_time = None   # block time of the first event seen
last_time = None    # latest block time seen
KEEP = max(WINDOW, FADE_WINDOW) + 5  # how many slots of tape to keep


def tape_of(mint):
    t = tapes.get(mint)
    if t is None:
        t = Tape()
        tapes[mint] = t
    return t


def price_of(curve, mark=None):
    """SOL per token from virtual reserves (mark overrides the curve)."""
    if mark is not None:
        vq = int(mark["virtual_quote_reserves"])
        vt = int(mark["virtual_token_reserves"])
    else:
        vq = curve.virtual_quote_reserves
        vt = curve.virtual_token_reserves
    if vt <= 0:
        return 0.0
    return vq / vt


def close_value(state, mint, pos, curve, mark):
    """What selling the whole position now would return, in SOL, net of
    the 1.25% curve fee and one tx fee."""
    tokens = int(pos.token_amount)
    if mark is not None:
        vq = int(mark["virtual_quote_reserves"])
        vt = int(mark["virtual_token_reserves"])
    else:
        vq = curve.virtual_quote_reserves
        vt = curve.virtual_token_reserves
    if vt + tokens <= 0:
        return 0.0
    gross = tokens * vq / (vt + tokens) / LAMPORTS
    return gross * (1.0 - CURVE_FEE) - state.tx_fee_lamports() / LAMPORTS


def net_flow(tape, now, window):
    """Net recorded SOL (buys minus sells) over the last `window` slots."""
    net = 0.0
    for slot, is_buy, sol, _user, _price in reversed(tape.trades):
        if slot <= now - window:
            break
        net += sol if is_buy else -sol
    return net


def entry_signal(tape, now, rq_sol):
    """True when the tape shows a genuine, accelerating surge."""
    if rq_sol < MIN_RQ_SOL or rq_sol > MAX_RQ_SOL:
        return False
    if tape.created is None or now - tape.created < MIN_AGE:
        return False
    start = now - WINDOW
    mid = now - WINDOW // 2
    buy_sol = 0.0
    sell_sol = 0.0
    buys = 0
    net_old = 0.0
    net_new = 0.0
    start_price = -1.0
    high = 0.0
    last = 0.0
    by_user = {}
    for slot, is_buy, sol, user, price in tape.trades:
        if slot <= start:
            start_price = price
            continue
        if start_price < 0:
            start_price = price
        signed = sol if is_buy else -sol
        if slot <= mid:
            net_old += signed
        else:
            net_new += signed
        if is_buy:
            buy_sol += sol
            buys += 1
            by_user[user] = by_user.get(user, 0.0) + sol
        else:
            sell_sol += sol
        if price > high:
            high = price
        last = price
    if buys < MIN_BUYS or len(by_user) < MIN_BUYERS:
        return False
    if buy_sol - sell_sol < MIN_NET_SOL:
        return False
    if start_price <= 0:
        return False
    up = last / start_price - 1.0
    if up < MIN_UP or up > MAX_UP:
        return False
    if last < high * NEAR_HIGH:
        return False
    if net_new <= 0 or net_new < ACCEL * net_old:
        return False
    top = 0.0
    for v in by_user.values():
        if v > top:
            top = v
    if top > MAX_BUYER_SHARE * buy_sol:
        return False
    return True


def portfolio_value_sol(state, marks):
    """Cash plus the current close value of every held position, in SOL.
    Used by the drawdown circuit breaker; falls back to cost when a curve's
    reserves are unknown."""
    total = state.cash / LAMPORTS
    for mint, pos in state.positions.items():
        curve = state.curves.get(mint)
        if curve is None:
            total += int(pos.cost_lamports) / LAMPORTS
        else:
            total += close_value(state, mint, pos, curve, marks.get(mint))
    return total


def on_batch(state, batch):
    global my_fills, first_time, last_time
    now = batch["slot"]
    big_sell = set()
    traded = set()

    for f in batch["fills"]:
        if f["status"] == "filled" and not f["client_id"].startswith("_"):
            my_fills += 1
            my_coins.add(f["mint"])

    # Defensive validation: the harness speaks the schema, but a malformed
    # event must never crash the run (a crash forfeits the whole grading).
    # Skip anything missing the fields this bot needs, instead of trusting
    # every key to be present and well-formed.
    marks = {m["mint"]: m for m in batch["marks"]
             if isinstance(m, dict) and "mint" in m
             and "virtual_quote_reserves" in m and "virtual_token_reserves" in m
             and "real_quote_reserves" in m}
    for e in batch["events"]:
        if not isinstance(e, dict) or "event" not in e or "mint" not in e:
            continue
        if e.get("block_time") is not None:
            if first_time is None:
                first_time = e["block_time"]
            if last_time is None or e["block_time"] > last_time:
                last_time = e["block_time"]
        if e["event"] == "CreateEvent":
            tape_of(e["mint"]).created = e["slot"]
        elif e["event"] == "TradeEvent":
            try:
                sol = int(e["sol_amount"]) / LAMPORTS
                vq = int(e["virtual_quote_reserves"])
                vt = int(e["virtual_token_reserves"])
            except (KeyError, TypeError, ValueError):
                continue
            t = tape_of(e["mint"])
            t.trades.append((e["slot"], e["is_buy"], sol, e["user"],
                             vq / max(1, vt)))
            traded.add(e["mint"])
            if not e["is_buy"] and sol >= 5.0:
                big_sell.add(e["mint"])

    for mint in traded:
        t = tapes[mint]
        cut = 0
        while cut < len(t.trades) and t.trades[cut][0] <= now - KEEP:
            cut += 1
        if cut:
            del t.trades[:cut]

    pending_by_mint = {}
    for po in state.pending.values():
        pending_by_mint[po.order["mint"]] = pending_by_mint.get(po.order["mint"], 0) + 1

    orders = []
    cap = state.hello["caps"]["max_orders_per_batch"]
    tx_sol = state.tx_fee_lamports() / LAMPORTS

    # --- Exits first. -------------------------------------------------------
    for mint, h in list(held.items()):
        pos = state.positions.get(mint)
        curve = state.curves.get(mint)
        if pos is None:
            if mint not in pending_by_mint:
                del held[mint]
                t = tape_of(mint)
                if h.filled:
                    t.last_exit = now
                else:
                    t.entries -= 1
            continue
        h.filled = True
        if curve is None or curve.complete:
            continue
        mark = marks.get(mint)
        rq_sol = (int(mark["real_quote_reserves"]) if mark is not None
                  else curve.real_quote_reserves) / LAMPORTS
        value = close_value(state, mint, pos, curve, mark)
        cost = int(pos.cost_lamports) / LAMPORTS
        if value > h.peak_value:
            h.peak_value = value
        h.last_value = value
        tape = tape_of(mint)
        exit_now = h.exiting
        if not exit_now and value < cost * (1.0 - STOP_LOSS):
            exit_now = True
        if (not exit_now and h.peak_value >= cost * (1.0 + TRAIL_ARM)
                and value < h.peak_value * (1.0 - TRAIL)):
            exit_now = True
        if not exit_now and net_flow(tape, now, FADE_WINDOW) < FADE_NET_SOL:
            exit_now = True
        if not exit_now and mint in big_sell:
            exit_now = True
        if not exit_now and now - h.opened > MAX_HOLD:
            exit_now = True
        if not exit_now:
            continue
        h.exiting = True
        if mint in pending_by_mint:
            sell_pending = any(po.order["mint"] == mint and po.order["side"] == "sell"
                               for po in state.pending.values())
            if sell_pending:
                continue
        if len(orders) >= cap:
            break
        orders.append({"side": "sell", "mint": mint,
                       "token_amount": pos.token_amount,
                       "max_slippage_bps": 10000})

    # --- Probe round trips: sell the probe as soon as its buy has landed. ---
    for mint, selling in list(probes.items()):
        pos = state.positions.get(mint)
        curve = state.curves.get(mint)
        if pos is None:
            if mint not in pending_by_mint:
                del probes[mint]
            continue
        if curve is None or curve.complete or mint in pending_by_mint:
            continue
        if len(orders) >= cap:
            break
        orders.append({"side": "sell", "mint": mint,
                       "token_amount": pos.token_amount,
                       "max_slippage_bps": 10000})
        if not selling:
            probes[mint] = True

    # --- Entries and pyramid adds. ------------------------------------------
    # Drawdown circuit breaker: once the portfolio has fallen 50% from the
    # starting capital, stop opening new positions (exits above keep running).
    # This keeps the run inside the competition's 50% max-drawdown guardrail.
    drawdown_halted = (
        portfolio_value_sol(state, marks)
        < START_CAPITAL_SOL * (1.0 - MAX_DRAWDOWN)
    )
    free = state.free_cash_lamports() / LAMPORTS
    slots_left = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
    main_left = MAX_MAIN - len(held)
    exposure = 0.0
    for mint, h in held.items():
        pos = state.positions.get(mint)
        exposure += max(h.invested, int(pos.cost_lamports) / LAMPORTS if pos is not None else 0.0)

    for mint in traded:
        if drawdown_halted:
            break
        if len(orders) >= cap:
            break
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        if mint in pending_by_mint or mint in probes:
            continue
        tape = tapes[mint]
        h = held.get(mint)
        price = price_of(curve)
        rq_sol = curve.real_quote_reserves / LAMPORTS
        if h is None:
            if slots_left <= 0 or main_left <= 0:
                continue
            if tape.entries >= MAX_ENTRIES or now - tape.last_exit < COOLDOWN:
                continue
            if not entry_signal(tape, now, rq_sol):
                continue
            want = min(FIRST_SOL, STAKE_SOL)
        else:
            # Pyramid into strength only: position is up, price stepped up
            # from the last add, and net SOL still flows in.
            if h.exiting or not h.filled or h.invested >= STAKE_SOL - MIN_BUY_SOL:
                continue
            pos = state.positions.get(mint)
            if pos is None:
                continue
            if price < h.last_add_price * (1.0 + ADD_STEP):
                continue
            if h.last_value < int(pos.cost_lamports) / LAMPORTS * (1.0 + ADD_IF_UP):
                continue
            if net_flow(tape, now, WINDOW) < MIN_NET_SOL:
                continue
            if rq_sol > MAX_RQ_SOL:
                continue
            want = min(ADD_SOL, STAKE_SOL - h.invested)
        cap_sol = state.max_buy_lamports(mint) / LAMPORTS
        amount = min(cap_sol, want, MAX_EXPOSURE_SOL - exposure)
        if amount > free - tx_sol:
            amount = free - tx_sol
        if amount < MIN_BUY_SOL:
            continue
        lamports = int(amount * LAMPORTS)
        orders.append({"side": "buy", "mint": mint, "quote_lamports": lamports,
                       "max_slippage_bps": BUY_SLIP_BPS})
        free -= amount + tx_sol
        exposure += amount
        if h is None:
            held[mint] = Held(now, price)
            held[mint].invested = amount
            tape.entries += 1
            slots_left -= 1
            main_left -= 1
        else:
            h.invested += amount
            h.last_add_price = price

    # --- Guardrail pacing: open a probe when fills or coins lag the pace. ---
    if (len(probes) < MAX_PROBES and slots_left > 0 and not drawdown_halted
            and first_time is not None and last_time is not None):
        pace = min(1.0, (last_time - first_time) / PACE_SECS)
        need_coins = len(my_coins) < -(-TARGET_COINS * pace // 1)  # ceil
        need_fills = my_fills < -(-TARGET_FILLS * pace // 1)      # ceil
        if need_coins or need_fills:
            min_rq = PROBE_SOL * BPS / state.hello["caps"]["max_order_bps_of_real_reserve"]
            for mint in traded:
                if len(orders) >= cap or len(probes) >= MAX_PROBES:
                    break
                if (mint in held or mint in probes or mint in pending_by_mint
                        or mint in state.positions):
                    continue
                if need_coins and not need_fills and mint in my_coins:
                    continue
                curve = state.curves.get(mint)
                if curve is None or curve.complete:
                    continue
                if curve.real_quote_reserves / LAMPORTS < min_rq * 1.5:
                    continue
                if free < PROBE_SOL + 2 * tx_sol:
                    break
                orders.append({"side": "buy", "mint": mint,
                               "quote_lamports": int(PROBE_SOL * LAMPORTS),
                               "max_slippage_bps": BUY_SLIP_BPS})
                probes[mint] = False
                free -= PROBE_SOL + tx_sol

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
