"""Unit tests for Pydantic domain schemas."""
import pytest
from datetime import date, timedelta
from pydantic import ValidationError
from models.schemas import BusinessProfile, Invoice, VatReturn, LedgerEntry


class TestBusinessProfileSchema:
    def test_valid_profile(self, valid_profile_data):
        profile = BusinessProfile(**valid_profile_data)
        assert profile.trn == "100123456789003"
        assert profile.legal_name == "Al Amal Trading LLC"

    @pytest.mark.parametrize("invalid_trn", [
        "123456789012345",  # Doesn't start with 100
        "10012345678900",   # 14 digits
        "1001234567890033", # 16 digits
        "10012345678900A", # Contains letter
        "",                 # Empty
    ])
    def test_trn_must_be_15_digits_starting_100(self, valid_profile_data, invalid_trn):
        data = valid_profile_data.copy()
        data["trn"] = invalid_trn
        with pytest.raises(ValidationError) as exc:
            BusinessProfile(**data)
        assert "TRN" in str(exc.value)

    def test_future_registration_date_rejected(self, valid_profile_data):
        data = valid_profile_data.copy()
        data["vat_registration_date"] = date.today() + timedelta(days=1)
        with pytest.raises(ValidationError) as exc:
            BusinessProfile(**data)
        assert "cannot be in the future" in str(exc.value)

    def test_invalid_email_rejected(self, valid_profile_data):
        data = valid_profile_data.copy()
        data["signatory_email"] = "not-an-email"
        with pytest.raises(ValidationError) as exc:
            BusinessProfile(**data)
        assert "email" in str(exc.value).lower()


class TestInvoiceSchema:
    def test_valid_invoice(self, valid_invoice_data):
        inv = Invoice(**valid_invoice_data)
        assert inv.invoice_number == "INV-2024-0091"
        assert inv.taxable_amount == 1000.0
        assert inv.vat_amount == 50.0
        assert inv.total == 1050.0

    def test_negative_amounts_rejected(self, valid_invoice_data):
        data = valid_invoice_data.copy()
        data["taxable_amount"] = -100.0
        with pytest.raises(ValidationError):
            Invoice(**data)

        data = valid_invoice_data.copy()
        data["vat_amount"] = -5.0
        with pytest.raises(ValidationError):
            Invoice(**data)

    def test_future_invoice_date_rejected(self, valid_invoice_data):
        data = valid_invoice_data.copy()
        data["date"] = date.today() + timedelta(days=5)
        with pytest.raises(ValidationError) as exc:
            Invoice(**data)
        assert "cannot be in the future" in str(exc.value)

    def test_mismatched_invoice_math_rejected(self, valid_invoice_data):
        data = valid_invoice_data.copy()
        data["taxable_amount"] = 1000.0
        data["vat_amount"] = 50.0
        data["total"] = 1200.0  # Incorrect total (should be 1050.0)
        with pytest.raises(ValidationError) as exc:
            Invoice(**data)
        assert "does not match" in str(exc.value)


class TestVatReturnSchema:
    def test_valid_vat_return(self, valid_vat_return_data):
        vr = VatReturn(**valid_vat_return_data)
        assert vr.period == "2024-Q1"
        assert vr.net_payable_refundable == -15000.0

    def test_invalid_period_order_rejected(self, valid_vat_return_data):
        data = valid_vat_return_data.copy()
        data["period_start"] = date(2024, 6, 1)
        data["period_end"] = date(2024, 3, 1)  # Before start
        with pytest.raises(ValidationError) as exc:
            VatReturn(**data)
        assert "cannot be before start date" in str(exc.value)

    def test_mismatched_net_vat_math_rejected(self, valid_vat_return_data):
        data = valid_vat_return_data.copy()
        data["output_tax"] = 1000.0
        data["input_tax"] = 500.0
        data["net_payable_refundable"] = 800.0  # Should be 500.0
        with pytest.raises(ValidationError) as exc:
            VatReturn(**data)
        assert "does not match" in str(exc.value)
