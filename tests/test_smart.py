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


# ---------------------------------------------------------------- memoria entre trozos y vocabulario

def test_trozos_cortos_cerrados_se_enlazan():
    f = ScriptFollower(GUION)
    assert f.feed(["hola"], final=True) is None          # una palabra corta sola no basta…
    assert f.feed(["todos"]) == _end_of(GUION, "todos")  # …pero enlazada con la siguiente, sí


def test_trozo_repetido_no_salta_a_la_frase_parecida():
    f = ScriptFollower(REPETIDA)
    f.feed(["el", "combate", "del", "juego", "se", "siente"])
    antes = f.position()
    assert f.feed(["el", "combate", "del", "juego", "se", "siente"]) is None
    assert f.feed(["el", "combate", "del", "juego", "se", "siente"], final=True) is None
    assert f.position() == antes < REPETIDA.index("Luego")


def test_lo_ya_usado_no_se_enlaza_con_la_frase_parecida():
    f = ScriptFollower(REPETIDA)
    f.feed(["el", "combate", "del", "juego"], final=True)
    antes = f.position()
    f.feed(["muy"])                                       # sigue en la primera frase, no en la de «muy ligero»
    assert antes <= f.position() < REPETIDA.index("Luego")


def test_vocabulario_del_guion():
    vocab = smart.script_vocabulary("Hola, Hola a todos. ¿Qué tal? Análisis.", "es")
    assert vocab[-1] == "[unk]"
    assert vocab.count("hola") == 1 and "análisis" in vocab and "qué" in vocab and "eh" in vocab

# ---------------------------------------------------------------- palabras que el modelo no conoce

_CONOCE = {"in", "jugable", "cinemática", "seis", "cinco", "ps", "o", "ene", "u", "pe", "ese", "dos", "mil",
           "veintiséis", "onu", "mp", "tres", "eme", "o", "ene", "u"}.__contains__


def test_numeros_en_palabras():
    assert smart.number_words(6) == ["seis"]
    assert smart.number_words(35) == ["treinta", "y", "cinco"]
    assert smart.number_words(2026) == ["dos", "mil", "veintiséis"]
    assert smart.number_words(1998) == ["mil", "novecientos", "noventa", "y", "ocho"]


def test_piezas_que_suenan_igual_y_siglas():
    assert smart.spoken_pieces("injugable", _CONOCE) == ["in", "jugable"]
    assert smart.spoken_pieces("cinemáticas", _CONOCE) == ["cinemática"]
    assert smart.spoken_pieces("mp3", _CONOCE) == ["mp", "tres"]
    assert smart.spoken_pieces("xyzzy", _CONOCE) is None
    assert smart.acronyms("La ONU 6 y el MP3. NUNCA les crean. Hola.") == {"onu", "mp3"}
    formas = smart.spoken_forms(["onu", "6", "injugable", "hola"], lambda w: w == "hola" or _CONOCE(w), "es", {"onu"})
    assert formas == {"onu": [["o", "ene", "u"]], "6": [["seis"]], "injugable": [["in", "jugable"]]}


def test_seguidor_junta_las_piezas_oidas():
    guion = "El juego es injugable en el MP3 y la ONU 6 sigue sin salir."
    f = ScriptFollower(guion)
    f.set_spoken_forms({"injugable": [["in", "jugable"]], "mp3": [["eme", "pe", "tres"]],
                        "onu": [["o", "ene", "u"]], "6": [["seis"]]})
    oido = ["el", "juego", "es", "in", "jugable"]
    assert f.feed(oido) == _end_of(guion, "injugable")
    oido += ["en", "el", "eme", "pe", "tres"]                # (el parcial va creciendo)
    assert f.feed(oido) == _end_of(guion, "MP3")
    assert f.feed(["y", "la", "o", "ene", "u", "seis", "sigue"], final=True) == _end_of(guion, "sigue")


def test_microfono_repetido_en_varios_sistemas_se_elige_por_numero(monkeypatch):
    """«Micrófono (D1)» sale en MME, DirectSound, WASAPI y WDM-KS: por el nombre solo, sounddevice fallaba."""
    import sys
    import types
    falso = types.SimpleNamespace(
        query_hostapis=lambda: [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}],
        query_devices=lambda i=None: (lambda lista: lista if i is None else lista[i])([
            {"name": "Asignador de sonido Microsoft - Input", "max_input_channels": 2, "hostapi": 0},
            {"name": "Micrófono (D1)", "max_input_channels": 1, "hostapi": 1},
            {"name": "Micrófono (D1)", "max_input_channels": 1, "hostapi": 0},
            {"name": "Micrófono (D1)", "max_input_channels": 1, "hostapi": 2},
        ]))
    monkeypatch.setitem(sys.modules, "sounddevice", falso)
    assert smart.device_index("Micrófono (D1)") == 2          # el de MME
    assert smart.device_index("") is None and smart.device_index(None) is None
    assert smart.device_index("Uno que ya no está") is None    # desconectado: el predeterminado
