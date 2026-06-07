"""Tests for SimpleTex formula OCR client."""

import pytest

from zotpilot.feature_extraction.formula_ocr import (
    SimpleTexFormulaOCR,
    is_high_quality_formula_latex,
)


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "status": True,
            "res": {
                "latex": r"\sigma = E\varepsilon",
                "conf": 0.97,
            },
            "request_id": "req-1",
        }


class _Client:
    last_call = None

    def __init__(self, *, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, endpoint, *, headers, data, files):
        _Client.last_call = {
            "endpoint": endpoint,
            "headers": headers,
            "data": data,
            "files": files,
            "timeout": self.timeout,
        }
        return _Response()


def test_simpletex_uat_token_request(monkeypatch):
    monkeypatch.setattr("zotpilot.feature_extraction.formula_ocr.httpx.Client", _Client)
    client = SimpleTexFormulaOCR(
        token="uat-token",
        endpoint="https://server.simpletex.cn/api/latex_ocr",
        request_interval_seconds=0,
    )

    result = client.recognize(b"png-bytes", filename="formula.png")

    assert result is not None
    assert result.latex == r"\sigma = E\varepsilon"
    assert result.confidence == 0.97
    assert _Client.last_call["headers"] == {"token": "uat-token"}
    assert _Client.last_call["files"]["file"][0] == "formula.png"


@pytest.mark.parametrize(
    "latex",
    [
        r"\sigma=\left[A_{JC}+B_{JC}\left(\varepsilon_{pl}\right)^{n_{JC}}\right]"
        r"\left[1+C_{JC}\ln\left(\frac{\dot{\varepsilon}_{pl}}{\dot{\varepsilon}_0}\right)\right]",
        r"(\tau+C_1\sigma_n)=C_2",
        r"\eta=\frac{\sigma_h}{\sigma_{VM}}",
        r"f_1=\sqrt{3J_2}+a_1I_1=(1+3a_1\eta)\sigma",
    ],
)
def test_formula_quality_accepts_real_math(latex):
    assert is_high_quality_formula_latex(latex)


@pytest.mark.parametrize(
    "latex",
    [
        r"\text{Al [C] Mg [C] Si [C] Fe [C] Cu [C] Mn [C]}",
        "214A.Giliolietal./InternationalJournalofImpactEngineering76(2015)207-220",
        r"\begin{aligned}&\text{https://doi.org/10.1016/example}\\&\text{Received 2025; Accepted 2026}\end{aligned}",
        "journalhomepage:www.elsevier.com/locate/engfracmech",
        "(11)",
        (
            r"\begin{aligned}&\text{Volumic mass}&&2700\text{ [Kg/m}^3]\\"
            r"&\text{Specific heat}&&0.89\text{ [J/KgK]}\\"
            r"&\text{Sound velocity}&&5350\text{ [m/s]}\end{aligned}"
        ),
        r"\begin{aligned}&\text{where }\zeta\text{ is the normalized third stress invariant}\end{aligned}",
        r"\boxed{\begin{array}{cccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" + ("c" * 420),
        r"\left(6\right)^3",
        (
            r"\begin{aligned}&\text{Table 2}\\"
            r"&\text{Identified parameters of the rate-dependent hardening law}\end{aligned}"
        ),
        r"\begin{aligned}&\text{用于韧性断裂预测的修正莫尔-库仑模型的力学}\\&\text{解释与推广}\end{aligned}",
    ],
)
def test_formula_quality_rejects_noise(latex):
    assert not is_high_quality_formula_latex(latex)
