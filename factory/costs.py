"""Kullanıcı tarifesinden USD maliyet tahmini; belirsiz kullanım sıfıra çevrilmez."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
import math
from typing import Any

# Tarife adı -> (kullanım birimi, fiyatın kapsadığı birim sayısı).
_COMPONENTS = {
    "base_per_request": ("requests", Decimal(1)),
    "input_per_million": ("input_tokens", Decimal(1_000_000)),
    "output_per_million": ("output_tokens", Decimal(1_000_000)),
    "per_second": ("seconds", Decimal(1)),
    "per_image": ("images", Decimal(1)),
    "per_character": ("characters", Decimal(1)),
}
_COUNT_UNITS = {"requests", "input_tokens", "output_tokens", "images", "characters"}
_ALLOWED_FIELDS = {*_COMPONENTS, "currency"}


def _nonnegative(value: Any) -> Decimal | None:
    """JSON sayıları ve hassas ondalık dizelerini kabul eder; bool sayı değildir."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def estimate_cost(
    provider: str,
    model: str,
    operation: str,
    usage: dict,
    rates: dict,
) -> float | None:
    """Açıkça yapılandırılmış tarife ve doğrulanmış birimlerle USD tahmini üretir.

    Şema: rates[provider][model][operation] = {"currency": "USD", ...tarife}.
    İşlem için tam eşleşme yoksa aynı model altındaki açık ``"*"`` tarifesi
    kullanılabilir. Sağlayıcı/model wildcard'ı ve implicit fiyat bulunmaz.

    Örnek tarife: {"input_per_million": 2, "output_per_million": 8}.
    Bu tarife hem ``usage.input_tokens`` hem ``usage.output_tokens`` gerektirir.
    Sadece tanımlı bileşenler toplanır; tarifede bulunmayan ücretler hesaplanmaz.
    Bilinmeyen birim, boş/yanlış tarife ve başka para birimi ``None`` döndürür.
    Açıkça sıfır fiyat veya doğrulanmış sıfır kullanım geçerli sıfırdır.
    """
    if not isinstance(rates, Mapping) or not isinstance(usage, Mapping):
        return None
    provider_rates = rates.get(provider)
    if not isinstance(provider_rates, Mapping):
        return None
    model_rates = provider_rates.get(model)
    if not isinstance(model_rates, Mapping):
        return None
    # Hatalı bir tam eşleşme, wildcard ile sessizce maskelenmez.
    tariff = model_rates.get(operation) if operation in model_rates else model_rates.get("*")
    if not isinstance(tariff, Mapping) or not tariff:
        return None
    if set(tariff) - _ALLOWED_FIELDS or tariff.get("currency", "USD") != "USD":
        return None
    configured = set(tariff) & _COMPONENTS.keys()
    if not configured:
        return None
    try:
        # Normal fatura ölçeklerinde ara yuvarlama yapmadan bileşenleri toplar.
        with localcontext() as decimal_context:
            decimal_context.prec = 38
            total = Decimal(0)
            for component in configured:
                unit, divisor = _COMPONENTS[component]
                rate = _nonnegative(tariff[component])
                quantity = _nonnegative(usage.get(unit))
                if rate is None or quantity is None:
                    return None
                if unit in _COUNT_UNITS and quantity != quantity.to_integral_value():
                    return None
                total += rate * quantity / divisor
            result = float(total)
    except (InvalidOperation, OverflowError, ArithmeticError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    # Float underflow da gerçek pozitif maliyeti sıfıra dönüştürmemeli.
    if total > 0 and result == 0:
        return None
    return result
