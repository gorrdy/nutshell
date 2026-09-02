"""Tests for `mint_melt_fee_percent`, the mint's own fee on outgoing payments.

The fee comes out of the Lightning fee reserve rather than on top of it: the
mint keeps whatever routing did not consume, capped at the configured percent,
and the rest goes back to the wallet as change. The quoted `fee_reserve` is
therefore the wallet's real price.
"""

from typing import Generator, List

import pytest
import pytest_asyncio

from cashu.core.base import Amount, MeltQuote, MeltQuoteState, Method, Unit
from cashu.core.helpers import (
    calculate_number_of_blank_outputs,
    fee_reserve,
    melt_fee,
    sum_proofs,
)
from cashu.core.models import PostMeltQuoteRequest
from cashu.core.settings import settings
from cashu.lightning.base import PaymentResult
from cashu.lightning.fake import FakeWallet
from cashu.mint.ledger import Ledger
from cashu.wallet.wallet import Wallet
from tests.conftest import SERVER_ENDPOINT
from tests.helpers import is_fake

MELT_FEE_PERCENT = 1.0
# kept well below FakeWallet's balance of 1337 sat
MELT_AMOUNT = 1_000
MELT_FEE = 10  # 1 % of MELT_AMOUNT

# Other test modules leave FakeWallet settings behind - a forced payment state,
# a raised exception flag - so everything this module relies on is pinned here.
BASELINE = {
    "fakewallet_payment_state": PaymentResult.SETTLED.name,
    "fakewallet_pay_invoice_state": PaymentResult.SETTLED.name,
    "fakewallet_payment_state_exception": False,
    "fakewallet_pay_invoice_state_exception": False,
    "fakewallet_delay_outgoing_payment": 0,
    "mint_input_fee_ppk": 0,
    "mint_melt_fee_percent": MELT_FEE_PERCENT,
}


@pytest.fixture(autouse=True)
def melt_fee_settings() -> Generator[None, None, None]:
    """Charge a melt fee and pin the baseline, restoring both afterwards.

    Keeps this module independent of the order tests run in.
    """
    previous = {key: getattr(settings, key) for key in BASELINE}
    for key, value in BASELINE.items():
        setattr(settings, key, value)
    # FakeWallet keeps its balance in a class attribute shared by every
    # instance, so paying out drains it for the tests that follow.
    previous_balance = FakeWallet.balance[Unit.sat]
    FakeWallet.balance[Unit.sat] = Amount(Unit.sat, 1_000_000)
    yield
    for key, value in previous.items():
        setattr(settings, key, value)
    FakeWallet.balance[Unit.sat] = previous_balance


@pytest_asyncio.fixture(scope="function")
async def wallet(ledger: Ledger):
    wallet = await Wallet.with_db(
        url=SERVER_ENDPOINT,
        db="test_data/wallet_mint_melt_fee",
        name="wallet_mint_melt_fee",
    )
    await wallet.load_mint()
    yield wallet


async def external_invoice(ledger: Ledger, amount: int) -> str:
    """A bolt11 request the mint has no mint quote for, so it is not internal."""
    invoice = await ledger.backends[Method.bolt11][Unit.sat].create_invoice(
        Amount(Unit.sat, amount)
    )
    assert invoice.payment_request
    return invoice.payment_request


def routing_reserve() -> int:
    """The Lightning fee reserve the backend quotes for `MELT_AMOUNT`."""
    amount_msat = Amount(Unit.sat, MELT_AMOUNT).to(Unit.msat).amount
    return Amount(Unit.msat, fee_reserve(amount_msat)).to(Unit.sat, round="up").amount


async def blank_outputs(wallet: Wallet, fee_reserve_sat: int) -> List:
    """The NUT-08 blank outputs a wallet sends along to receive change."""
    n_outputs = calculate_number_of_blank_outputs(fee_reserve_sat)
    secrets, rs, _ = await wallet.generate_n_secrets(n_outputs, skip_bump=True)
    outputs, _ = wallet._construct_outputs(n_outputs * [1], secrets, rs)
    return outputs


def test_melt_fee_helper():
    assert melt_fee(10_000) == 100
    # rounded up, so that small payments are not free
    assert melt_fee(1) == 1
    assert melt_fee(0) == 0


def test_melt_fee_helper_disabled():
    settings.mint_melt_fee_percent = 0.0
    assert melt_fee(10_000) == 0


def _quote(amount: int, fee_reserve: int, fee_paid: int = 0) -> MeltQuote:
    return MeltQuote(
        quote="quote",
        method=Method.bolt11.name,
        request="lnbc",
        checking_id="checking_id",
        unit=Unit.sat.name,
        amount=amount,
        fee_reserve=fee_reserve,
        fee_paid=fee_paid,
        state=MeltQuoteState.unpaid,
    )


def test_get_melt_fee_is_the_reserve_left_after_routing(ledger: Ledger):
    """The mint keeps the reserve minus routing, so the user pays the quote."""
    quote = _quote(amount=10_000, fee_reserve=100, fee_paid=10)
    assert ledger.get_melt_fee(quote, fee_reserve_provided=100) == 90


def test_get_melt_fee_is_capped_at_the_percent(ledger: Ledger):
    """A reserve floor above the percent must not become the price.

    Small payments are dominated by `lightning_reserve_fee_min`; without the
    cap a 100 sat payment would pay the whole 5 sat floor.
    """
    quote = _quote(amount=100, fee_reserve=5, fee_paid=1)
    assert ledger.get_melt_fee(quote, fee_reserve_provided=5) == 1


def test_get_melt_fee_never_negative(ledger: Ledger):
    """Routing eating the whole reserve leaves the mint with nothing."""
    quote = _quote(amount=10_000, fee_reserve=100, fee_paid=100)
    assert ledger.get_melt_fee(quote, fee_reserve_provided=100) == 0
    quote = _quote(amount=10_000, fee_reserve=100, fee_paid=120)
    assert ledger.get_melt_fee(quote, fee_reserve_provided=100) == 0


def test_get_melt_fee_disabled(ledger: Ledger):
    settings.mint_melt_fee_percent = 0.0
    quote = _quote(amount=10_000, fee_reserve=100, fee_paid=10)
    assert ledger.get_melt_fee(quote, fee_reserve_provided=100) == 0


@pytest.mark.asyncio
@pytest.mark.skipif(not is_fake, reason="only fakewallet")
async def test_melt_quote_is_not_inflated_by_the_melt_fee(ledger: Ledger):
    """The quote stays the plain routing reserve: that is the user's price."""
    request = await external_invoice(ledger, MELT_AMOUNT)
    quote = await ledger.melt_quote(
        PostMeltQuoteRequest(unit=Unit.sat.name, request=request)
    )

    assert quote.amount == MELT_AMOUNT
    assert quote.fee_reserve == routing_reserve()


@pytest.mark.asyncio
@pytest.mark.skipif(not is_fake, reason="only fakewallet")
async def test_melt_quote_internal_stays_free(wallet: Wallet, ledger: Ledger):
    """A payment inside the mint pays neither routing nor melt fee."""
    mint_quote = await wallet.request_mint(MELT_AMOUNT)
    quote = await ledger.melt_quote(
        PostMeltQuoteRequest(unit=Unit.sat.name, request=mint_quote.request)
    )
    assert quote.amount == MELT_AMOUNT
    assert quote.fee_reserve == 0


@pytest.mark.asyncio
@pytest.mark.skipif(not is_fake, reason="only fakewallet")
async def test_melt_keeps_melt_fee_and_returns_the_rest(wallet: Wallet, ledger: Ledger):
    """The mint keeps the melt fee out of the reserve; the rest is change."""
    request = await external_invoice(ledger, MELT_AMOUNT)
    quote = await ledger.melt_quote(
        PostMeltQuoteRequest(unit=Unit.sat.name, request=request)
    )
    assert quote.fee_reserve == routing_reserve()

    # the wallet has to provide the full quoted fee reserve up front
    provided = MELT_AMOUNT + quote.fee_reserve
    mint_quote = await wallet.request_mint(provided)
    proofs = await wallet.mint(amount=provided, quote_id=mint_quote.quote)
    assert sum_proofs(proofs) == provided

    outputs = await blank_outputs(wallet, quote.fee_reserve)
    response = await ledger.melt(proofs=proofs, quote=quote.quote, outputs=outputs)

    assert response.state == MeltQuoteState.paid.value
    assert response.change is not None
    change = sum(promise.amount for promise in response.change)

    melted_quote = await ledger.get_melt_quote(quote.quote)
    # FakeWallet reports a routing fee of 1 sat
    assert melted_quote.fee_paid == 1
    assert change == quote.fee_reserve - MELT_FEE - melted_quote.fee_paid
    # what the mint kept is exactly the melt fee
    assert quote.fee_reserve - change - melted_quote.fee_paid == MELT_FEE
    # and the quote was the ceiling: a wallet holding exactly amount +
    # fee_reserve can pay, which is the whole point of charging out of it
    total_paid = MELT_AMOUNT + quote.fee_reserve - change
    assert total_paid <= MELT_AMOUNT + quote.fee_reserve
    assert total_paid == MELT_AMOUNT + MELT_FEE + melted_quote.fee_paid


@pytest.mark.asyncio
@pytest.mark.skipif(not is_fake, reason="only fakewallet")
async def test_melt_without_melt_fee_returns_full_reserve(
    wallet: Wallet, ledger: Ledger
):
    """With the fee disabled the mint behaves exactly as upstream."""
    settings.mint_melt_fee_percent = 0.0

    request = await external_invoice(ledger, MELT_AMOUNT)
    quote = await ledger.melt_quote(
        PostMeltQuoteRequest(unit=Unit.sat.name, request=request)
    )
    assert quote.fee_reserve == routing_reserve()

    provided = MELT_AMOUNT + quote.fee_reserve
    mint_quote = await wallet.request_mint(provided)
    proofs = await wallet.mint(amount=provided, quote_id=mint_quote.quote)

    outputs = await blank_outputs(wallet, quote.fee_reserve)
    response = await ledger.melt(proofs=proofs, quote=quote.quote, outputs=outputs)

    assert response.change is not None
    change = sum(promise.amount for promise in response.change)
    melted_quote = await ledger.get_melt_quote(quote.quote)
    assert change == quote.fee_reserve - melted_quote.fee_paid


@pytest.mark.asyncio
@pytest.mark.skipif(not is_fake, reason="only fakewallet")
async def test_melt_fee_does_not_shrink_the_routing_budget(
    wallet: Wallet, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
):
    """The backend gets the whole reserve as its fee limit.

    The melt fee is settled only after the payment, out of what routing left
    over, so charging it must not cost the payment any routing headroom.
    """
    request = await external_invoice(ledger, MELT_AMOUNT)
    quote = await ledger.melt_quote(
        PostMeltQuoteRequest(unit=Unit.sat.name, request=request)
    )
    assert quote.fee_reserve == routing_reserve()

    provided = MELT_AMOUNT + quote.fee_reserve
    mint_quote = await wallet.request_mint(provided)
    proofs = await wallet.mint(amount=provided, quote_id=mint_quote.quote)

    captured = {}
    backend = ledger.backends[Method.bolt11][Unit.sat]
    pay_invoice = backend.pay_invoice

    async def capturing_pay_invoice(melt_quote, fee_limit_msat):
        captured["fee_limit_msat"] = fee_limit_msat
        return await pay_invoice(melt_quote, fee_limit_msat)

    monkeypatch.setattr(backend, "pay_invoice", capturing_pay_invoice)
    response = await ledger.melt(proofs=proofs, quote=quote.quote, outputs=None)

    assert response.state == MeltQuoteState.paid.value
    expected = Amount(Unit.sat, routing_reserve()).to(Unit.msat).amount
    assert captured["fee_limit_msat"] == expected
