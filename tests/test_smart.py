"""Pruebas del modo Smart (cronista/teleprompter/smart.py). Sin micrófono ni internet."""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from cronista.teleprompter import smart
from cronista.teleprompter.smart import ScriptFollower, guess_language, normalize, tokenize

GUION = (
    "Hola a todos y bienvenidos a este análisis de Rise of Iron. "
    "Hoy vamos a hablar de cómo se siente el combate en la primera parte, con su ritmo lento "
    "y pesado. El juego apuesta por las coberturas, y cada disparo cuenta. Si os fijáis en el "
    "escenario, veréis que los muros marcan el paso de cada zona. Después repasaremos la "
    "campaña, los enemigos y la sensación de peso al recargar. Al final os diré cuál me parece "
    "la mejor parte del juego, y por qué."
)

ASSETS = Path(r"F:\Reclaimer\tests\assets")


def _end_of(text: str, phrase: str) -> int:
    """Carácter donde termina la última palabra de `phrase` dentro de `text`."""
    return text.index(phrase) + len(phrase)


# ---------------------------------------------------------------- texto

def test_normalize_quita_tildes_puntuacion_y_mayusculas():
    assert normalize("Análisis") == "analisis"
    assert normalize("¡Hola!") == "hola"
    assert normalize("Ñandú") == "nandu"
    assert normalize("Müller,") == "muller"
    assert normalize("2026") == "2026"
    assert normalize("...") == ""


def test_tokenize_devuelve_posiciones_del_texto_original():
    texto = "Hola, ¿qué tal? Análisis."
    tokens = tokenize(texto)
    assert [t[0] for t in tokens] == ["hola", "que", "tal", "analisis"]
    for norm, ini, fin in tokens:
        assert normalize(texto[ini:fin]) == norm
    assert texto[tokens[1][1]:tokens[1][2]] == "qué"


def test_tokenize_ignora_tokens_vacios():
    assert tokenize("— ... ¿? !!") == []


def test_guess_language_es_en_y_por_defecto():
    assert guess_language("Esta es la prueba de que el texto va en español y no en otro idioma.") == "es"
    assert guess_language("This is the test and it is for the English text of the script.") == "en"
    assert guess_language("") == "es"


# ---------------------------------------------------------------- ScriptFollower

def test_avanza_con_parciales_crecientes_y_es_monotono():
    f = ScriptFollower(GUION)
    palabras = [w for w, _, _ in tokenize(GUION)]
    posiciones = []
    for k in range(1, len(palabras) + 1):
        f.feed(palabras[max(0, k - 6):k])
        posiciones.append(f.position())
    assert all(b >= a for a, b in zip(posiciones, posiciones[1:]))
    assert posiciones[-1] > 0.9 * len(GUION)


def test_parcial_inicial_mueve_al_encajar_dos_palabras():
    f = ScriptFollower(GUION)
    assert f.feed(["hola"]) is None          # una palabra corta no basta
    assert f.feed(["hola", "a", "todos"]) == _end_of(GUION, "todos")
    assert f.position() == _end_of(GUION, "todos")
    assert f.feed(["hola", "a", "todos", "bienvenidos"]) == _end_of(GUION, "bienvenidos")


def test_repetir_el_mismo_parcial_no_mueve_nada():
    f = ScriptFollower(GUION)
    f.feed(["hola", "a", "todos"])
    assert f.feed(["hola", "a", "todos"]) is None
    assert f.position() == _end_of(GUION, "todos")


def test_muletillas_no_mueven_el_texto():
    f = ScriptFollower(GUION)
    f.feed(["hola", "a", "todos"])
    antes = f.position()
    assert f.feed(["eh", "pues", "mmm"]) is None
    assert f.feed(["vale", "entonces", "yo", "creo"]) is None
    assert f.position() == antes


def test_palabra_larga_sola_basta_si_esta_cerca():
    f = ScriptFollower(GUION)
    f.reset(GUION.index("las coberturas"))
    assert f.feed(["coberturas"]) == _end_of(GUION, "coberturas")


def test_palabra_larga_sola_no_salta_lejos():
    f = ScriptFollower(GUION)
    assert f.feed(["coberturas"]) is None
    assert f.position() == 0


REPETIDA = (
    "El combate del juego se siente muy pesado al principio de la aventura. "
    "Luego hablamos de la historia, de sus personajes principales y de cómo cambia todo con calma. "
    "El combate del juego se siente muy ligero cuando termina la campaña."
)


def test_frase_parecida_de_mas_adelante_no_gana_a_la_actual():
    f = ScriptFollower(REPETIDA)
    assert f.feed(["el", "combate", "del", "juego", "se", "siente", "muy"]) == _end_of(REPETIDA, "siente muy")
    assert f.position() < REPETIDA.index("Luego")


def test_frase_parecida_con_una_palabra_mal_oida_no_salta():
    f = ScriptFollower(REPETIDA)
    f.feed(["el", "combate", "del", "juego"])
    antes = f.position()
    # lo mismo que se acaba de decir, sin nada nuevo: encaja igual aquí que en la frase de más adelante
    assert f.feed(["combate", "del", "juego", "se"]) == _end_of(REPETIDA, "juego se")
    assert antes < f.position() < REPETIDA.index("Luego")


def test_dos_palabras_que_estan_mas_adelante_no_saltan():
    f = ScriptFollower(GUION)
    f.feed(["hola", "a", "todos"])
    antes = f.position()
    assert f.feed(["sensacion", "de"]) is None
    assert f.feed(["los", "muros"]) is None
    assert f.position() == antes


def test_saltarse_un_trozo_con_una_frase_larga_si_mueve():
    f = ScriptFollower(GUION)
    f.feed(["hola", "a", "todos"])
    oido = ["repasaremos", "la", "campaña", "los", "enemigos", "y", "la", "sensacion"]
    posicion = None
    for _ in range(7):                       # lo lejano solo se busca tras varios intentos sin encajar cerca
        posicion = f.feed(oido) or posicion
    assert posicion == _end_of(GUION, "sensación")


def test_palabras_mal_reconocidas_siguen_encajando():
    f = ScriptFollower(GUION)
    assert f.feed(["hola", "a", "todos", "bienbenidos"]) == _end_of(GUION, "bienvenidos")
    # «analisis» sin tilde encaja con «análisis» porque la normalización quita tildes
    assert f.feed(["este", "analisis"]) == _end_of(GUION, "análisis")


def test_no_salta_hacia_atras():
    f = ScriptFollower(GUION)
    f.reset(GUION.index("se siente"))
    f.feed(["se", "siente", "el", "combate"])
    avanzado = f.position()
    assert avanzado == _end_of(GUION, "el combate")
    assert f.feed(["hola", "a", "todos"]) is None
    assert f.position() == avanzado


def test_reset_recoloca_el_lector():
    f = ScriptFollower(GUION)
    f.feed(["hola", "a", "todos"])
    f.reset(GUION.index("El juego"))
    assert f.position() == GUION.index("El juego")
    assert f.feed(["el", "juego", "apuesta"]) == _end_of(GUION, "apuesta")
    idx, total = f.progress_words()
    assert total == len(tokenize(GUION))
    assert idx > 0


def test_reset_hacia_atras_permite_volver_a_leer():
    f = ScriptFollower(GUION)
    f.feed(["la", "mejor", "parte", "del", "juego"])
    f.reset(0)
    assert f.position() == 0
    assert f.feed(["hola", "a", "todos"]) == _end_of(GUION, "todos")


def test_guion_vacio_no_rompe():
    f = ScriptFollower("")
    assert f.feed(["hola", "a"]) is None
    assert f.progress_words() == (0, 0)


# ---------------------------------------------------------------- modelos

def test_model_path_none_para_idioma_desconocido():
    assert smart.model_path("fr") is None


def test_models_coinciden_con_lo_pedido():
    assert smart.MODELS["es"][0] == "vosk-model-small-es-0.42"
    assert smart.MODELS["en"][0] == "vosk-model-small-en-us-0.15"


# ---------------------------------------------------------------- integración con Vosk

def test_integracion_narracion_real():
    wav_path = ASSETS / "narracion_prueba.wav"
    json_path = ASSETS / "narracion_prueba.transcripcion.json"
    modelo = smart.model_path("es")
    if modelo is None:
        pytest.skip("Modelo de voz español no descargado.")
    if not wav_path.exists() or not json_path.exists():
        pytest.skip("Falta el audio o la transcripción de prueba.")

    import json

    import vosk

    guion = " ".join(w[2] for w in json.loads(json_path.read_text(encoding="utf-8"))["words"])

    vosk.SetLogLevel(-1)
    with wave.open(str(wav_path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        rate = w.getframerate()
        rec = vosk.KaldiRecognizer(vosk.Model(str(modelo)), rate)
        follower = ScriptFollower(guion)
        anterior = 0
        palabras_oidas: list[str] = []
        while True:
            data = w.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                # Segmento terminado: sus palabras finales y luego empieza otro.
                follower.feed(json.loads(rec.Result()).get("text", "").split())
                palabras_oidas = []
            else:
                palabras_oidas = json.loads(rec.PartialResult()).get("partial", "").split()
                follower.feed(palabras_oidas)
            assert follower.position() >= anterior
            anterior = follower.position()
        follower.feed(json.loads(rec.FinalResult()).get("text", "").split())

    assert follower.position() >= anterior
    porcentaje = follower.position() / len(guion)
    print(f"\nPosición final: {follower.position()} de {len(guion)} ({porcentaje:.0%})")
    assert porcentaje > 0.70
