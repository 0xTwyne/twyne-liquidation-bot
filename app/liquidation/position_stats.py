"""Per-vault position stats fetched from HealthStatViewer.positionStats()."""

from dataclasses import dataclass

MAXFACTOR = 1e4
USD = 1e18


@dataclass
class PositionStats:
    collateral_native: float
    collateral_usd: float
    reserved_native: float
    reserved_usd: float
    borrow_native: float
    borrow_usd: float
    twyne_liq_ltv_pct: float  # ~LTV_t (capped param)
    twyne_ltv_pct: float  # LTV_t = B/C (unbounded)
    external_liq_ltv_pct: float  # ~LTV_e
    external_ltv_pct: float  # LTV_e = B/(C+C_LP)
    in_hf: float
    ext_hf: float
    max_twyne_ltv_pct: float = 0.0  # ~LTV_t^max (protocol cap); twyne_liq_ltv = min(chosen, this)
    collateral_symbol: str = ""
    debt_symbol: str = ""

    @classmethod
    def from_raw(cls, raw, *, collateral_decimals, collateral_symbol, debt_decimals, debt_symbol):
        (c_nat, c_usd, r_nat, r_usd, b_nat, b_usd, liq_t, ltv_t, liq_e, ltv_e, ext_hf, in_hf, max_liq_t) = raw
        cdiv = 10**collateral_decimals
        ddiv = 10**debt_decimals
        return cls(
            collateral_native=c_nat / cdiv,
            collateral_usd=c_usd / USD,
            reserved_native=r_nat / cdiv,
            reserved_usd=r_usd / USD,
            borrow_native=b_nat / ddiv,
            borrow_usd=b_usd / USD,
            twyne_liq_ltv_pct=liq_t / MAXFACTOR * 100,
            twyne_ltv_pct=ltv_t / MAXFACTOR * 100,
            external_liq_ltv_pct=liq_e / MAXFACTOR * 100,
            external_ltv_pct=ltv_e / MAXFACTOR * 100,
            in_hf=in_hf / USD,
            ext_hf=ext_hf / USD,
            max_twyne_ltv_pct=max_liq_t / MAXFACTOR * 100,
            collateral_symbol=collateral_symbol,
            debt_symbol=debt_symbol,
        )

    def render_table(self) -> str:
        """Vertical key/value table for single-vault alerts (Slack code block)."""
        cs, ds = self.collateral_symbol, self.debt_symbol
        rows = [
            ("C  (collateral)", f"{self.collateral_native:,.4f} {cs}", f"${self.collateral_usd:,.2f}"),
            ("C_LP (reserved)", f"{self.reserved_native:,.4f} {cs}", f"${self.reserved_usd:,.2f}"),
            ("B  (borrow)", f"{self.borrow_native:,.4f} {ds}", f"${self.borrow_usd:,.2f}"),
            ("LTV_t", f"{self.twyne_ltv_pct:.2f}%", ""),
            ("~LTV_t", f"{self.twyne_liq_ltv_pct:.2f}%", ""),
            ("LTV_e", f"{self.external_ltv_pct:.2f}%", ""),
            ("~LTV_e", f"{self.external_liq_ltv_pct:.2f}%", ""),
            ("inHF", f"{self.in_hf:.4f}", ""),
            ("extHF", f"{self.ext_hf:.4f}", ""),
        ]
        w0 = max(len(r[0]) for r in rows)
        w1 = max(len(r[1]) for r in rows)
        body = "\n".join(f"{a:<{w0}}  {b:<{w1}}  {c}".rstrip() for a, b, c in rows)
        return f"```\n{body}\n```"

    def render_row(self, *, vault: str, spy_link: str) -> str:
        """One compact line for the bulk report table."""
        return (
            f"`{vault}` "
            f"C=${self.collateral_usd:,.0f} B=${self.borrow_usd:,.0f} "
            f"LTV_t={self.twyne_ltv_pct:.1f}/{self.twyne_liq_ltv_pct:.1f} "
            f"LTV_e={self.external_ltv_pct:.1f}/{self.external_liq_ltv_pct:.1f} "
            f"inHF={self.in_hf:.3f} extHF={self.ext_hf:.3f} "
            f"<{spy_link}|Spy>"
        )
