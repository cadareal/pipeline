from recibo.textnorm import norm, split_leading_code


def test_norm_removes_accents_and_case():
    assert norm("Previdência  do Regime Estatutário") == "PREVIDENCIA DO REGIME ESTATUTARIO"
    assert norm(None) == ""
    assert norm("  Saúde ") == "SAUDE"


def test_split_leading_code():
    assert split_leading_code("10 - SAUDE") == ("10", "SAUDE")
    assert split_leading_code("272 . PREVIDENCIA DO REGIME ESTATUTARIO") == ("272", "PREVIDENCIA DO REGIME ESTATUTARIO")
    assert split_leading_code("SAUDE") == (None, "SAUDE")
    assert split_leading_code("2024 TOTAL") == (None, "2024 TOTAL")
