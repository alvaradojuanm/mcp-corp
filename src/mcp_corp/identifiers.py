"""Normalización y validación de identificadores fiscales venezolanos.

Módulo dedicado, sin dependencia de FastMCP ni de ningún conector: recibe
lo que el usuario/modelo escribió (con puntos, guiones, con o sin letra,
mayúscula o minúscula) y devuelve una forma canónica estructurada. Cada
conector decide, a partir de esa forma canónica, qué representación
concreta necesita su fuente — este módulo no sabe nada de Postgres ni de
HTTP.

Dominio (verificado contra fuentes, ver README para el detalle):
- Cédula: 8 dígitos, SIN dígito verificador.
- RIF: letra + 8 dígitos + 1 dígito verificador (checksum módulo 11).
- Para personas naturales (letra `V`), los 8 dígitos del RIF SON el número
  de cédula — no son dos números distintos.
- Prefijos oficiales: `V` (natural venezolano), `E` (natural extranjero),
  `J` (jurídica), `G` (entidad gubernamental), `P` (pasaporte). La letra
  `I` NO EXISTE en el registro del SENIAT — es un error que arrastran
  varias librerías y regex publicados; ver `test_identifiers.py` para el
  test de regresión explícito de esta trampa.
- `C` (comunas/consejos comunales/organizaciones del Poder Popular) existe
  desde un anuncio oficial de 2015, pero las fuentes consultadas difieren
  sobre si sigue vigente en el set que usa el portal actual del SENIAT.
  Por eso queda detrás de un flag (`incluir_prefijo_c`), no cableada.

Validar el dígito verificador ANTES de tocar cualquier conector es la
razón de ser de este módulo en el diseño de resiliencia: rechaza un
identificador mal tipeado sin gastar un slot del semáforo, sin abrir una
conexión del pool, sin llegar a ninguna fuente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Prefijos oficiales verificados. NO incluye "I": no existe en el
# registro del SENIAT (ver docstring del módulo).
PREFIJOS_BASE: frozenset[str] = frozenset({"V", "E", "J", "G", "P"})

# "C" (comunas / consejos comunales / Poder Popular): existe desde un
# anuncio oficial de 2015, pero su vigencia actual en el set del SENIAT es
# ambigua entre las fuentes consultadas. Se ofrece como opt-in explícito.
PREFIJO_C = "C"

# Prefijo por defecto cuando el usuario no escribe ninguna letra: se
# asume persona natural venezolana, el caso más común.
PREFIJO_POR_DEFECTO = "V"

# Peso por posición (izquierda a derecha) para las 8 cifras del número,
# y valor base por letra, en la fórmula de módulo 11 del SENIAT.
# Verificado cruzando tres implementaciones independientes (un gist de
# python-venezuela, joseayram/utils en PHP, y la librería
# django-localflavor-ve) que coinciden exactamente, y confirmado contra
# un ejemplo real conocido: V-13222105-3 dio suma=63, residuo=8,
# verificador=3 con esta misma fórmula. Ver README para el detalle.
_PESOS_POSICION: tuple[int, ...] = (3, 2, 7, 6, 5, 4, 3, 2)
_BASE_POR_LETRA: dict[str, int] = {
    "V": 4,
    "E": 8,
    "J": 12,
    "P": 16,
    "G": 20,
    # "C" comparte fórmula con "J" según joseayram/utils Rif.php (única
    # fuente que documenta un valor para C); no se pudo corroborar contra
    # un segundo ejemplo real conocido con letra C. Se deja aquí a
    # propósito para no romper `calcular_digito_verificador` si algún día
    # se activa `incluir_prefijo_c`, pero queda marcado como de menor
    # confianza que el resto de la tabla.
    "C": 12,
}


class IdentificadorInvalidoError(Exception):
    """El valor no es un identificador fiscal venezolano reconocible.

    Se levanta ANTES de tocar cualquier conector — nunca consume un slot
    de concurrencia ni abre una conexión. El mensaje es siempre de
    negocio, apto para mostrarse tal cual (nunca incluye el valor
    original completo para no repetir en el error algo que el propio
    usuario ya escribió mal, ni detalle interno alguno).
    """


@dataclass(frozen=True)
class IdentidadFiscal:
    """Forma canónica de un identificador fiscal venezolano ya normalizado."""

    prefijo: str
    numero: str  # siempre 8 dígitos, con ceros a la izquierda si hacía falta
    digito_verificador: str | None  # None si el valor de entrada no traía uno

    @property
    def cedula(self) -> str:
        """Forma canónica corta: prefijo + 8 dígitos, sin verificador.

        Para personas naturales (`V`) esto ES el número de cédula. Es la
        forma que usa, p. ej., el conector de Postgres en esta fase.
        """
        return f"{self.prefijo}{self.numero}"

    @property
    def rif(self) -> str:
        """Forma canónica completa de RIF: prefijo-8dígitos-verificador.

        Levanta `ValueError` si no se conoce (o no se pudo calcular) un
        dígito verificador para esta identidad.
        """
        if self.digito_verificador is None:
            raise ValueError("no se conoce el dígito verificador de esta identidad")
        return f"{self.prefijo}-{self.numero}-{self.digito_verificador}"


def prefijos_habilitados(*, incluir_c: bool = False) -> frozenset[str]:
    """Set de prefijos que `normalizar` acepta, según configuración."""
    return PREFIJOS_BASE | ({PREFIJO_C} if incluir_c else frozenset())


def normalizar(valor: str, *, prefijos: frozenset[str] = PREFIJOS_BASE) -> IdentidadFiscal:
    """Acepta cualquiera de los formatos equivalentes y devuelve la forma canónica.

    Formatos aceptados (equivalentes entre sí): con o sin puntos de millar,
    con o sin guiones, con o sin letra de prefijo, mayúscula o minúscula.
    Ej.: "16760320", "16.760.320", "V16760320", "V-16760320",
    "V-16.760.320" son todos la misma cédula; "J-167603200",
    "J16.760.320-0" y "J-16760320-0" son el mismo RIF.

    Levanta `IdentificadorInvalidoError` para cualquier valor no
    reconocible. Nunca toca ninguna fuente de datos: es pura
    transformación de texto.
    """
    if not isinstance(valor, str) or not valor.strip():
        raise IdentificadorInvalidoError("identificador vacío o de tipo inválido")

    # Los separadores (puntos de millar, guiones, espacios) son puramente
    # cosméticos en todos los formatos aceptados: se eliminan por completo
    # antes de interpretar letra y dígitos, en vez de intentar un regex
    # único que contemple todas las posiciones donde pueden aparecer.
    limpio = valor.strip().upper().replace(".", "").replace("-", "").replace(" ", "")

    letra_match = re.match(r"^[A-Z]", limpio)
    if letra_match:
        letra = letra_match.group()
        resto = limpio[1:]
    else:
        letra = PREFIJO_POR_DEFECTO
        resto = limpio

    if not resto.isdigit():
        raise IdentificadorInvalidoError("formato de identificador no reconocido")

    if letra not in prefijos:
        raise IdentificadorInvalidoError(f"prefijo '{letra}' no es un prefijo fiscal válido")

    if len(resto) == 9:
        # 8 dígitos del número + 1 dígito verificador de RIF.
        numero, verificador = resto[:8], resto[8]
    elif 1 <= len(resto) <= 8:
        # Cédula (o RIF sin verificador): relleno con cero a la izquierda
        # si el número tiene menos de 8 dígitos.
        numero, verificador = resto.zfill(8), None
    else:
        raise IdentificadorInvalidoError("cantidad de dígitos fuera de rango")

    return IdentidadFiscal(prefijo=letra, numero=numero, digito_verificador=verificador)


def calcular_digito_verificador(prefijo: str, numero: str) -> str:
    """Calcula el dígito verificador (módulo 11) para `prefijo` + `numero`.

    `numero` debe ser una cadena de exactamente 8 dígitos. Levanta
    `ValueError` si no hay fórmula conocida para `prefijo`.
    """
    if prefijo not in _BASE_POR_LETRA:
        raise ValueError(f"no hay fórmula de dígito verificador para el prefijo '{prefijo}'")
    if len(numero) != 8 or not numero.isdigit():
        raise ValueError("numero debe ser una cadena de exactamente 8 dígitos")

    suma = sum(int(digito) * peso for digito, peso in zip(numero, _PESOS_POSICION))
    suma += _BASE_POR_LETRA[prefijo]
    residuo = suma % 11
    valor = 11 - residuo
    return str(valor if valor < 10 else 0)


def digito_verificador_es_valido(identidad: IdentidadFiscal) -> bool:
    """True si `identidad` no trae verificador, o si el que trae es correcto.

    Deliberado: la ausencia de verificador (una cédula sin RIF completo)
    NO es un fallo de validación — es el caso normal para cédulas. Solo
    se rechaza cuando el verificador SÍ vino y no coincide con el
    calculado.
    """
    if identidad.digito_verificador is None:
        return True
    return calcular_digito_verificador(identidad.prefijo, identidad.numero) == identidad.digito_verificador
