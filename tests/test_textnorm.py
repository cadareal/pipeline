from recibo.textnorm import norm, normalizar_familia, split_leading_code


def test_norm_removes_accents_and_case():
    assert norm("Previdência  do Regime Estatutário") == "PREVIDENCIA DO REGIME ESTATUTARIO"
    assert norm(None) == ""
    assert norm("  Saúde ") == "SAUDE"


def test_split_leading_code():
    assert split_leading_code("10 - SAUDE") == ("10", "SAUDE")
    assert split_leading_code("272 . PREVIDENCIA DO REGIME ESTATUTARIO") == ("272", "PREVIDENCIA DO REGIME ESTATUTARIO")
    assert split_leading_code("SAUDE") == (None, "SAUDE")
    assert split_leading_code("2024 TOTAL") == (None, "2024 TOTAL")


def test_normalizar_familia_so_mexe_em_sigla_minuscula():
    assert normalizar_familia("ntn-i") == "NTN-I" and normalizar_familia("lft") == "LFT"
    assert normalizar_familia("NTN-I") == "NTN-I" and normalizar_familia("Euro") == "Euro" and normalizar_familia("Global BRL") == "Global BRL"
    assert normalizar_familia("BIRD9172-BR") == "BIRD9172-BR" and normalizar_familia("tda24010300") == "tda24010300" and normalizar_familia("") == ""
