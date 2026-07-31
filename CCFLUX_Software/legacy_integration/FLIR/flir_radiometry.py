#!/usr/bin/env python3
"""FLIR radiometric count-to-temperature conversion.

The equations follow Teledyne FLIR's ``counts2temp`` reference calculation for
radiometric A-series streams using R, B, F, J0, J1, X, alpha1, alpha2, beta1,
and beta2 camera calibration values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np


CALIBRATION_FIELDS = (
    "R",
    "B",
    "F",
    "J0",
    "J1",
    "X",
    "alpha1",
    "alpha2",
    "beta1",
    "beta2",
)


@dataclass(frozen=True)
class CorrectionInputs:
    """Measured scene/environment inputs for quantitative thermography."""

    emissivity: float
    object_distance_m: float
    atmospheric_temperature_c: float
    reflected_apparent_temperature_c: float
    relative_humidity_percent: float
    external_optics_transmission: float = 1.0
    external_optics_temperature_c: float | None = None

    def validate(self) -> None:
        if not 0 < self.emissivity <= 1:
            raise ValueError("emissivity must be in (0, 1]")
        if self.object_distance_m < 0:
            raise ValueError("object_distance_m must be non-negative")
        if not 0 <= self.relative_humidity_percent <= 100:
            raise ValueError("relative_humidity_percent must be in [0, 100]")
        if not 0 < self.external_optics_transmission <= 1:
            raise ValueError("external_optics_transmission must be in (0, 1]")
        for name, value in (
            ("atmospheric_temperature_c", self.atmospheric_temperature_c),
            (
                "reflected_apparent_temperature_c",
                self.reflected_apparent_temperature_c,
            ),
        ):
            if not np.isfinite(value) or value <= -273.15:
                raise ValueError(f"{name} must be above absolute zero")
        if (
            self.external_optics_temperature_c is not None
            and (
                not np.isfinite(self.external_optics_temperature_c)
                or self.external_optics_temperature_c <= -273.15
            )
        ):
            raise ValueError(
                "external_optics_temperature_c must be above absolute zero"
            )


def validate_calibration(calibration: Mapping[str, Any]) -> dict[str, float]:
    missing = [
        name for name in CALIBRATION_FIELDS if calibration.get(name) is None
    ]
    if missing:
        raise ValueError("missing calibration values: " + ",".join(missing))
    values = {name: float(calibration[name]) for name in CALIBRATION_FIELDS}
    if not all(np.isfinite(value) for value in values.values()):
        raise ValueError("calibration values must be finite")
    if values["R"] <= 0 or values["B"] <= 0 or values["J1"] == 0:
        raise ValueError("R and B must be positive and J1 must be non-zero")
    return values


def blackbody_pseudo_radiance(
    temperature_kelvin: float | np.ndarray,
    calibration: Mapping[str, Any],
) -> np.ndarray:
    values = validate_calibration(calibration)
    temperature = np.asarray(temperature_kelvin, dtype=np.float64)
    if np.any(temperature <= 0):
        raise ValueError("temperature_kelvin must be positive")
    denominator = np.exp(values["B"] / temperature) - values["F"]
    result = np.full(temperature.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(denominator) & (denominator > 0)
    result[valid] = values["R"] / denominator[valid]
    return result


def atmospheric_transmission(
    inputs: CorrectionInputs,
    calibration: Mapping[str, Any],
) -> tuple[float, float]:
    """Return (transmission, absolute humidity proxy in g/m^3)."""
    inputs.validate()
    values = validate_calibration(calibration)
    atmospheric_c = inputs.atmospheric_temperature_c
    relative_humidity = inputs.relative_humidity_percent / 100.0
    water_content = relative_humidity * np.exp(
        1.5587
        + 0.06939 * atmospheric_c
        - 0.00027816 * atmospheric_c**2
        + 0.00000068455 * atmospheric_c**3
    )
    sqrt_distance = np.sqrt(inputs.object_distance_m)
    sqrt_water = np.sqrt(max(float(water_content), 0.0))
    transmission = values["X"] * np.exp(
        -sqrt_distance
        * (values["alpha1"] + values["beta1"] * sqrt_water)
    ) + (1.0 - values["X"]) * np.exp(
        -sqrt_distance
        * (values["alpha2"] + values["beta2"] * sqrt_water)
    )
    if not np.isfinite(transmission) or not 0 < transmission <= 1.000001:
        raise ValueError(
            f"atmospheric transmission is outside the physical range: {transmission}"
        )
    return min(float(transmission), 1.0), float(water_content)


def counts_to_temperature(
    raw_counts: np.ndarray,
    calibration: Mapping[str, Any],
    inputs: CorrectionInputs | None = None,
) -> tuple[np.ndarray, dict[str, float | str]]:
    """Convert a raw count array to apparent or corrected temperature in degC."""
    values = validate_calibration(calibration)
    counts = np.asarray(raw_counts, dtype=np.float64)
    data_radiance = (counts - values["J0"]) / values["J1"]

    if inputs is None:
        emissivity = 1.0
        transmission = 1.0
        optics_transmission = 1.0
        water_content = 0.0
        reflected_term = 0.0
        atmospheric_term = 0.0
        optics_term = 0.0
        method = "apparent_blackbody_temperature"
    else:
        inputs.validate()
        emissivity = inputs.emissivity
        transmission, water_content = atmospheric_transmission(
            inputs, calibration
        )
        optics_transmission = inputs.external_optics_transmission
        reflected_kelvin = inputs.reflected_apparent_temperature_c + 273.15
        atmospheric_kelvin = inputs.atmospheric_temperature_c + 273.15
        optics_temperature_c = (
            inputs.external_optics_temperature_c
            if inputs.external_optics_temperature_c is not None
            else inputs.atmospheric_temperature_c
        )
        optics_kelvin = optics_temperature_c + 273.15
        reflected_radiance = float(
            blackbody_pseudo_radiance(reflected_kelvin, calibration)
        )
        atmospheric_radiance = float(
            blackbody_pseudo_radiance(atmospheric_kelvin, calibration)
        )
        optics_radiance = float(
            blackbody_pseudo_radiance(optics_kelvin, calibration)
        )
        reflected_term = ((1.0 - emissivity) / emissivity) * reflected_radiance
        atmospheric_term = (
            (1.0 - transmission) / (emissivity * transmission)
        ) * atmospheric_radiance
        optics_term = (
            (1.0 - optics_transmission)
            / (emissivity * transmission * optics_transmission)
        ) * optics_radiance
        method = "flir_full_environment_corrected_temperature"

    correction_sum = reflected_term + atmospheric_term + optics_term
    object_radiance = (
        data_radiance
        / (emissivity * transmission * optics_transmission)
        - correction_sum
    )
    log_argument = np.full(object_radiance.shape, np.nan, dtype=np.float64)
    valid_radiance = np.isfinite(object_radiance) & (object_radiance > 0)
    log_argument[valid_radiance] = (
        values["R"] / object_radiance[valid_radiance] + values["F"]
    )
    temperature = np.full(object_radiance.shape, np.nan, dtype=np.float64)
    valid = valid_radiance & np.isfinite(log_argument) & (log_argument > 1)
    temperature[valid] = (
        values["B"] / np.log(log_argument[valid]) - 273.15
    )
    diagnostics: dict[str, float | str] = {
        "method": method,
        "atmospheric_transmission": transmission,
        "water_content_g_m3": water_content,
        "reflected_radiance_term": reflected_term,
        "atmospheric_radiance_term": atmospheric_term,
        "external_optics_radiance_term": optics_term,
        "invalid_temperature_fraction": float(
            np.count_nonzero(~np.isfinite(temperature)) / temperature.size
        ),
        "equation": (
            "radiance=(DN-J0)/J1; "
            "K2=reflected+atmosphere+external_optics; "
            "T=B/ln(R/(radiance/(emissivity*tau*optics_tau)-K2)+F)-273.15"
        ),
    }
    return temperature, diagnostics

