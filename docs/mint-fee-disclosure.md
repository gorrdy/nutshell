# Draft: Mint fee disclosure for minting and melting

**Status:** draft proposal, not an adopted NUT. Written for discussion in
`cashubtc/nuts`.

**Authors:** operators of cashu.cz

## Summary

Mints have no way to charge a disclosed fee for Lightning operations, and
wallets have no way to learn the price of one. NUT-02 lets a mint advertise
`input_fee_ppk` for swaps, but NUT-04 (minting) and NUT-05 (melting) have no
equivalent. A mint that wants to charge for an outgoing payment today has to
hide the fee inside `fee_reserve`, which conflates two unrelated things: a
refundable routing budget and a non-refundable price.

This proposal adds:

1. an optional `fee` descriptor per method/unit in `GetInfoResponse`, so a
   wallet can price an operation *before* requesting a quote, and
2. an optional `mint_fee` field in the mint and melt quote responses, stating
   how much of the quoted total the mint keeps regardless of what the
   Lightning payment actually costs.

Both are additive and optional. `fee_reserve` keeps its current meaning, so no
existing wallet changes behaviour or breaks.

## Motivation

### `fee_reserve` is a budget, not a price

NUT-05 defines `fee_reserve` as an amount the wallet must provide on top of
`amount`, and NUT-08 returns the unused part as change. It is therefore an
upper bound on cost, not a cost. Measured on a production mint (cashu.cz,
Nutshell 0.20.3, LND backend, 105 melts over 910,023 sat, all with the default
1 % reserve and no mint fee):

| | share of volume |
|---|---|
| quoted `fee_reserve` | 1.014 % |
| Lightning routing actually paid | 0.057 % |

Routing consumed 5.7 % of the quoted reserve; the other 94.3 % was never
needed. The reserve overstates the real cost by roughly 18x. It has to: it is sized
for the worst route, not the typical one. A wallet that displays it as "fee"
is showing a number that is almost never the price, and a wallet cannot tell
the difference between a mint that refunds the reserve and one that keeps it.

### Charging a fee today forces a bad trade-off

A mint that wants 1 % for outgoing payments has two options, and both are
defects:

**Add the fee on top of the reserve.** The quote asks for
`amount + 1 % + 1 %`. A wallet holding `amount + 1 %` is then refused a
payment it can actually afford, and "send my whole balance" stops working. We
shipped this first and it was the immediate objection from the first person we
showed it to.

**Withhold the fee from the change.** The quote stays at `amount + 1 %`, and
the mint keeps what routing did not consume. Nothing over-charges and no
wallet breaks, but the price is now invisible: a mint quoting 1 % and keeping
1 % is indistinguishable from a mint quoting 1 % and keeping nothing. It also
leans on a gap in NUT-08, which says the mint imprints `overpaid_fees` into the
blank outputs without stating that it must be the whole difference.

The second option is what this proposal formalises: it is the right behaviour,
it just needs to be declared.

### Wallets cannot do arithmetic they need to do

Without a disclosed fee a wallet cannot:

- show the user what an outgoing payment costs,
- compute the largest amount a balance can send,
- compare two mints on price,
- or decide whether a payment is worth making at all.

Note also that a wallet cannot treat the reserve as reliably refundable even
today: the mint returns only as many change outputs as the wallet supplied
(`n_return_outputs = min(len(outputs), len(return_amounts))`), so a wallet that
sends too few blank outputs silently forfeits the rest. On our mint that has
amounted to 1,914 sat across its history, entirely by accident.

## Specification

### 1. Fee descriptor in `GetInfoResponse`

A mint MAY add a `fee` object to any method entry of the NUT-04 and NUT-05
settings:

```json
"nuts": {
  "5": {
    "methods": [
      {
        "method": "bolt11",
        "unit": "sat",
        "min_amount": 0,
        "max_amount": 1000000,
        "fee": {
          "ppm": 10000,
          "fixed": 0,
          "max": null
        }
      }
    ],
    "disabled": false
  }
}
```

| field | type | meaning |
|---|---|---|
| `ppm` | `int` | proportional part, parts per million of `amount` |
| `fixed` | `int` | flat part, in the method's unit |
| `max` | `int \| null` | optional cap on the total fee, in the unit |

The fee for an operation of `amount` is:

```
raw  = fixed + ceil(amount * ppm / 1_000_000)
fee  = raw if max is null else min(raw, max)
```

Integer arithmetic and `ceil` are normative: mint and wallet must reach the
same number without agreeing on floating point behaviour. `ppm` rather than
NUT-02's `ppk` because 0.1 % is too coarse a step for Lightning pricing.

An absent `fee` object means the mint discloses nothing, not that the fee is
zero. `{"ppm": 0, "fixed": 0}` is how a mint states that an operation is free.

The descriptor is advisory: the quote is authoritative. A wallet MUST NOT
assume the descriptor and the quote agree, and MUST use the quote when
deciding whether to proceed.

### 2. `mint_fee` in the melt quote response

```json
{
  "quote": "<str>",
  "request": "<str>",
  "amount": 10000,
  "unit": "sat",
  "fee_reserve": 110,
  "mint_fee": 100,
  "method": "bolt11",
  "state": "UNPAID",
  "expiry": 1700000000
}
```

`mint_fee` is the part of `fee_reserve` that the mint keeps regardless of what
routing costs. Rules:

- `mint_fee` MUST be `<= fee_reserve`. The wallet's obligation is unchanged:
  it provides at least `amount + fee_reserve + fee` (NUT-02 input fee), and
  never more than today.
- A mint MUST NOT keep more than the quoted `mint_fee`.
- A mint SHOULD size `fee_reserve` as `mint_fee` plus routing headroom, so
  that a route more expensive than expected reduces the mint's own take
  rather than failing the payment.
- An absent `mint_fee` MUST be treated as unknown, not as zero: it means the
  mint does not disclose the split, exactly as today.

NUT-08 change becomes unambiguous:

```
change = fee_reserve_provided - lightning_fee_paid - mint_fee     (floor 0)
```

### 3. `mint_fee` in the mint quote response

For an incoming payment the fee is charged by making the payment request
larger than the ecash to be issued:

```json
{
  "quote": "<str>",
  "request": "lnbc...",
  "amount": 10000,
  "unit": "sat",
  "mint_fee": 100,
  "state": "UNPAID",
  "expiry": 1700000000
}
```

- `amount` remains the ecash the wallet will receive and MUST equal the sum of
  the blinded outputs, unchanged from NUT-04.
- The payment request is for `amount + mint_fee`.
- A wallet that validates the request amount MUST accept
  `amount + mint_fee`, not only `amount`.

The alternative — request for `amount`, issue `amount - mint_fee` — was
rejected: wallets keep the amount they asked for and construct outputs for it,
so the mint's `sum(outputs) == quote.amount` check rejects the mint operation
after the user has already paid. That is the worst possible failure mode.

### 4. Wallet behaviour

- SHOULD present `mint_fee` as the price of the operation, and
  `fee_reserve - mint_fee` as a temporary hold that is expected back.
- MUST NOT assume that change equals `fee_reserve - lightning_fee_paid`.
- When computing the largest sendable amount, MUST solve for `amount` using
  the advertised descriptor rather than assuming the reserve is refundable.
- SHOULD send the full NUT-08 blank output set; a short set forfeits change.

## Backwards compatibility

| | behaviour |
|---|---|
| old wallet, new mint | ignores `mint_fee`, provides `amount + fee_reserve` as before, receives less change. Works; the displayed maximum fee is pessimistic. |
| new wallet, old mint | `mint_fee` absent, treated as unknown, wallet behaves as today. |
| new wallet, new mint | price is visible before and inside the quote. |

No field changes meaning and no field is removed, so this can ship as an
optional NUT without a coordinated upgrade.

## Open questions

1. **New NUT, or amendments to 04/05/06/08?** The fee descriptor touches
   NUT-06 settings, `mint_fee` touches NUT-04 and NUT-05, and the change
   formula clarifies NUT-08. A single optional NUT that defines the shapes and
   references the others may be cleaner than four edits.
2. **`ppm` vs NUT-02's `ppk`.** Consistency argues for `ppk`, precision for
   `ppm`. Reconciling would mean touching NUT-02.
3. **`fixed` vs a separate `min`.** `fixed` covers the "small payments must
   not be free" case; a distinct `min` may still be wanted.
4. **Internal settlements.** When a melt targets an invoice the same mint
   issued, no Lightning is involved. A mint MAY charge; ours quotes
   `mint_fee: 0` and `fee_reserve: 0`. Worth stating explicitly.
5. **Does any wallet validate the mint quote request amount?** Section 3
   depends on the answer. A survey of cashu.me, Minibits, Nutstash and
   Boardwalk is needed before it can be recommended.
6. **Is a melt fee in scope for Cashu at all?** There is a reasonable position
   that fees belong at the swap layer (`input_fee_ppk`) and nowhere else. If
   that is the consensus, the disclosure half of this proposal still stands:
   mints that charge via the reserve exist either way, and wallets should be
   able to see it.

## Reference implementation

This branch. `MINT_MELT_FEE_PERCENT` (default `0.0`, i.e. upstream behaviour)
implements the melt half of section 2 — the fee comes out of the reserve,
capped at the configured percent — without the disclosure fields, which is
precisely the gap this proposal closes.

| where | what |
|---|---|
| `cashu/core/settings.py` | `mint_melt_fee_percent` |
| `cashu/core/helpers.py` | `melt_fee()`, the fee formula |
| `cashu/mint/ledger.py` | `Ledger.get_melt_fee()`, and the two places that withhold it from the change |
| `tests/mint/test_mint_melt_fee.py` | 11 tests, including one that asserts the backend still receives the *whole* reserve as its `fee_limit_msat` |

Notes on the implementation, in case they are useful to anyone specifying
this:

- The fee is **not stored**. It is derived from the quote and the routing fee
  that was actually paid, because a column on `melt_quotes` would mean a
  migration. What the mint kept for a melt stays derivable as
  `fee_reserve - fee_paid - sum(change)`.
- The percent is a **cap, not a price**, so that `lightning_reserve_fee_min`
  does not become the price of small payments. Without the cap a 100 sat
  payment pays the whole 5 sat floor (7.4 % measured, against 2.0 % with it).
- The backend receives the **full** `fee_reserve` as its fee limit. Charging
  the fee must not cost a payment any routing headroom, and a test pins this.
- Internal settlements — a melt of an invoice the same mint issued — never
  reach the backend and are free.

Running on cashu.cz since 2026-09-03.
