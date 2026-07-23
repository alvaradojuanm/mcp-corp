"""Unitarios de `identifiers.py`: normalización y checksum del RIF venezolano.

El algoritmo del dígito verificador se verificó cruzando tres
implementaciones independientes (un gist de python-venezuela, el paquete
PHP joseayram/utils, y la librería django-localflavor-ve) que coinciden
exactamente, y se confirmó contra un ejemplo real conocido y verificado
externamente: V-13222105-3 ("Rif correcto" según el documento fuente),
usado como caso base en varios de estos tests.
"""

from __future__ import annotations

import pytest

from mcp_corp.identifiers import (
    PREFIJO_C,
    PREFIJOS_BASE,
    IdentidadFiscal,
    IdentificadorInvalidoError,
    calcular_digito_verificador,
    digito_verificador_es_valido,
    normalizar,
    prefijos_habilitados,
)

# --- Formatos equivalentes de cédula ------------------------------------

CEDULA_FORMATOS_EQUIVALENTES = [
    "16760320",
    "16.760.320",
    "V16760320",
    "V-16760320",
    "V-16.760.320",
    "v16760320",  # minúscula
    "v-16.760.320",
]


@pytest.mark.parametrize("valor", CEDULA_FORMATOS_EQUIVALENTES)
def test_formatos_de_cedula_normalizan_al_mismo_canonico(valor: str) -> None:
    identidad = normalizar(valor)
    assert identidad == IdentidadFiscal(prefijo="V", numero="16760320", digito_verificador=None)
    assert identidad.cedula == "V16760320"


# --- Formatos equivalentes de RIF ----------------------------------------

RIF_FORMATOS_EQUIVALENTES = [
    "J-167603200",
    "J16.760.320-0",
    "J-16760320-0",
    "j167603200",
]


@pytest.mark.parametrize("valor", RIF_FORMATOS_EQUIVALENTES)
def test_formatos_de_rif_normalizan_al_mismo_canonico(valor: str) -> None:
    identidad = normalizar(valor)
    assert identidad == IdentidadFiscal(prefijo="J", numero="16760320", digito_verificador="0")
    assert identidad.rif == "J-16760320-0"


# --- Cédula vs RIF: el verificador distingue el caso ---------------------


def test_cedula_sin_letra_no_trae_digito_verificador() -> None:
    identidad = normalizar("16760320")
    assert identidad.digito_verificador is None
    with pytest.raises(ValueError):
        _ = identidad.rif  # no se puede pedir la forma RIF sin verificador


def test_rif_completo_si_trae_digito_verificador() -> None:
    identidad = normalizar("V-13222105-3")
    assert identidad.digito_verificador == "3"
    assert identidad.rif == "V-13222105-3"


def test_persona_natural_v_el_numero_de_rif_es_la_cedula() -> None:
    """Regla de dominio: para prefijo V, los 8 dígitos del RIF SON la cédula."""
    identidad_rif = normalizar("V-16760320-0")
    identidad_cedula = normalizar("16760320")
    assert identidad_rif.numero == identidad_cedula.numero
    assert identidad_rif.cedula == identidad_cedula.cedula


# --- Prefijos válidos, y el rechazo explícito de "I" ---------------------


@pytest.mark.parametrize("letra", sorted(PREFIJOS_BASE))
def test_prefijos_oficiales_aceptados(letra: str) -> None:
    identidad = normalizar(f"{letra}16760320")
    assert identidad.prefijo == letra


def test_letra_i_es_rechazada_no_existe_en_el_seniat() -> None:
    """Test de regresión de la trampa conocida: 'I' NO es un prefijo válido.

    Varias librerías y regex publicados heredan por error la letra 'I'
    como si fuera el prefijo de extranjero (el correcto es 'E'). Si esta
    prueba empieza a fallar, alguien reintrodujo el error.
    """
    with pytest.raises(IdentificadorInvalidoError):
        normalizar("I16760320")


def test_prefijo_c_deshabilitado_por_defecto() -> None:
    with pytest.raises(IdentificadorInvalidoError):
        normalizar("C16760320")  # prefijos por defecto = PREFIJOS_BASE, sin C


def test_prefijo_c_habilitable_explicitamente() -> None:
    habilitados = prefijos_habilitados(incluir_c=True)
    assert PREFIJO_C in habilitados
    identidad = normalizar("C16760320", prefijos=habilitados)
    assert identidad.prefijo == "C"


def test_prefijo_desconocido_es_rechazado() -> None:
    for letra in ("A", "B", "F", "H", "Z"):
        with pytest.raises(IdentificadorInvalidoError):
            normalizar(f"{letra}16760320")


def test_sin_letra_asume_v_por_defecto() -> None:
    assert normalizar("16760320").prefijo == "V"


# --- Relleno con cero -----------------------------------------------------


@pytest.mark.parametrize(
    ("entrada", "numero_esperado"),
    [
        ("123456", "00123456"),
        ("1234567", "01234567"),
        ("1", "00000001"),
        ("V123456", "00123456"),
    ],
)
def test_relleno_con_cero_para_numeros_cortos(entrada: str, numero_esperado: str) -> None:
    assert normalizar(entrada).numero == numero_esperado


# --- Entradas inválidas: rechazadas, nunca tocan una fuente ---------------


@pytest.mark.parametrize(
    "valor",
    [
        "",
        "   ",
        "abc",
        "123456789012",  # demasiados dígitos, sin letra
        "V1234567890",  # demasiados dígitos, con letra (más de 9)
        "V",  # solo letra, sin número
        "12-34-56-78-90",  # separadores en posiciones sin sentido -> 10 dígitos
    ],
)
def test_valores_invalidos_levantan_error_sin_tocar_ninguna_fuente(valor: str) -> None:
    with pytest.raises(IdentificadorInvalidoError):
        normalizar(valor)


def test_valor_no_string_levanta_error() -> None:
    with pytest.raises(IdentificadorInvalidoError):
        normalizar(12345678)  # type: ignore[arg-type]


# --- Dígito verificador: algoritmo verificado contra fuente externa ------


def test_calculo_coincide_con_ejemplo_real_verificado_externamente() -> None:
    # V-13222105-3: documentado como "Rif correcto" en una fuente externa
    # (cálculo paso a paso: suma=63, residuo=8, verificador=11-8=3).
    assert calcular_digito_verificador("V", "13222105") == "3"


def test_digito_verificador_es_valido_para_el_ejemplo_externo() -> None:
    identidad = normalizar("V-13222105-3")
    assert digito_verificador_es_valido(identidad) is True


def test_digito_verificador_detecta_valor_incorrecto() -> None:
    identidad = normalizar("V-13222105-9")  # mismo número, verificador equivocado
    assert digito_verificador_es_valido(identidad) is False


def test_ausencia_de_verificador_no_es_un_fallo_de_validacion() -> None:
    """Una cédula sin RIF completo (sin verificador) es el caso normal, no un error."""
    identidad = normalizar("16760320")
    assert digito_verificador_es_valido(identidad) is True


def test_calcular_digito_verificador_sin_formula_para_el_prefijo_levanta_value_error() -> None:
    with pytest.raises(ValueError):
        calcular_digito_verificador("Z", "16760320")
