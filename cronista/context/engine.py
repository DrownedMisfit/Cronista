"""Motor de contexto (fase 2 — versión preliminar heurística, sustituible).

Devuelve una clasificación en dos dimensiones (FUNCIÓN y TEMA), el ÁMBITO
(juego actual / otro juego / tema general / pensamiento personal), referencias
externas detectadas y una confianza. Nunca inventa: si no hay indicios suficientes
marca `ambiguous=True` y confianza baja. Mantiene un estado contextual de sesión
(tema actual, última actividad, referencias) para el panel lateral.

La versión definitiva usará un modelo de lenguaje local; esta implementación
solo mira indicadores lingüísticos y es deliberadamente conservadora.
"""
from __future__ import annotations

import re
import threading
from typing import Any

FUNCTIONS = ["descripción", "análisis", "opinión", "crítica", "comparación", "pensamiento", "observación",
             "idea para video"]
TOPICS = ["gameplay", "mecánicas", "combate", "historia", "narrativa", "personajes", "diseño de niveles",
          "dirección artística", "gráficos", "animaciones", "música", "sonido", "rendimiento", "ia", "ritmo",
          "diálogos", "exploración", "dificultad", "interfaz", "cámara", "controles"]
SCOPES = {"current": "Videojuego actual", "other": "Otro videojuego", "general": "Tema general",
          "personal": "Pensamiento personal", "": "Sin clasificar"}

_FUNC_PATTERNS: dict[str, list[str]] = {
    "descripción": [r"\b(estoy (entrando|llegando|viendo|en)|acabo de (llegar|entrar)|lo que (estoy viendo|veo) es|"
                    r"en esta parte|ahora (estamos|estoy)|hay (un|una|unos|unas|muchos|varios)|se puede ver|"
                    r"voy a describir|aquí (hay|se ve|aparece)|a los lados|al fondo|delante|detrás)\b"],
    "comparación": [r"\b(me recuerda (mucho |muchísimo |bastante )?a|es parecido a|se parece a|igual que en|"
                    r"como en|al estilo de|comparado con|a diferencia de|mejor que en|peor que en|lo mismo que)\b"],
    "crítica": [r"\b(el problema (es|está|real)|no funciona|me molesta|está mal|falla|abusa|demasiado|"
                r"se siente (lento|torpe|pesado|vacío|injusto)|no tiene sentido|mal (diseñad|hech)|"
                r"no me gusta|es frustrante|incómodo|inconsistente|incoherente|sin sentido)\b"],
    "análisis": [r"\b(creo que (funciona|el problema|esto|aquí)|funciona porque|porque (el juego|la mecánica|esto)|"
                 r"lo interesante (aquí )?es|lo que (hace|logra|consigue) (el juego|esto)|esto (hace|provoca|genera|logra) que|"
                 r"el diseño|la razón|esto significa|se nota que|la intención)\b"],
    "opinión": [r"\b(me (gusta|encanta|parece|flipa)|creo que|siento que|pienso que|a mí|para mí|"
                r"me parece (bien|mal|interesante|genial|increíble|aburrido)|está (muy |bastante )?(bien|genial|increíble|bonito))\b"],
    "pensamiento": [r"\b(no sé (cómo explicarlo|por qué|cómo decirlo)|ahora que lo pienso|me hace pensar|"
                    r"cuando (era|jugaba) (pequeño|niño|chico)|de pequeño|me pregunto|quizá|tal vez|a lo mejor|"
                    r"no sé si|estoy pensando)\b"],
    "idea para video": [r"\b(idea para (un |el )?video|para el video|en el guion|para el guion|podría hablar de|"
                        r"esto da para (un video|hablar)|tema para (un )?video|lo voy a usar)\b"],
    "observación": [r"\b(me (he )?(fijado|di cuenta)|fíjate|curioso que|ojo (con|a)|nótese|date cuenta|"
                    r"me llama la atención)\b"],
}
_TOPIC_PATTERNS: dict[str, list[str]] = {
    "combate": [r"\b(combat\w*|pelea\w*|enemig\w+|disparo\w*|arma\w*|munici\w+|golpe\w*|boss|jefe\w*|cobertura\w*|recarga\w*|apunt\w+)\b"],
    "mecánicas": [r"\b(mec[aá]nic\w+|sistema de|habilidad\w*|salto\w*|esquiv\w+|parry|stamina|inventario|crafteo|progresi[oó]n|desbloque\w+)\b"],
    "gameplay": [r"\b(gameplay|jugabilidad|controles|se siente (jugar|moverse)|input|movimiento|moverse)\b"],
    "diseño de niveles": [r"\b(zona\w*|nivel\w*|mapa\w*|escenario\w*|sección\w*|área\w*|espacio\w*|distribuci[oó]n|"
                          r"ruta\w*|pasillo\w*|abierto\w*|lineal\w*|dise[ñn]o de nivel\w*|arena\w*|explorar|exploraci[oó]n)\b"],
    "personajes": [r"\b(personaje\w*|protagonista\w*|villano\w*|aliado\w*|compañer\w+|actor\w*|carisma\w*)\b"],
    "historia": [r"\b(historia|trama|argumento|cap[ií]tulo\w*|giro\w*|final\w*|lore|cinem[aá]tica\w*|escena\w*)\b"],
    "narrativa": [r"\b(narrativ\w+|narra\w*|ludonarrativ\w+|disonancia|cuenta la historia|storytelling|tono)\b"],
    "diálogos": [r"\b(di[aá]logo\w*|doblaje|voces|frase\w* que dice|l[ií]nea\w* de di[aá]logo|guion)\b"],
    "dirección artística": [r"\b(direcci[oó]n art[ií]stica|arte|est[eé]tica|paleta|iluminaci[oó]n|ambient\w+|atm[oó]sfera|colores?)\b"],
    "gráficos": [r"\b(gr[aá]fic\w+|textura\w*|resoluci[oó]n|modelado|efectos|part[ií]culas|sombras|reflejos)\b"],
    "animaciones": [r"\b(animaci[oó]n\w*|animad\w+|movimiento\w* (del|de la) (personaje|cámara))\b"],
    "música": [r"\b(m[uú]sica\w*|banda sonora|soundtrack|tema musical|melod[ií]a\w*|compositor\w*)\b"],
    "sonido": [r"\b(sonido\w*|audio|efectos de sonido|mezcla|ambiente sonoro|se escucha|suena)\b"],
    "rendimiento": [r"\b(rendimiento|fps|frames?|tir[oó]n\w*|stutter\w*|cargas?|tiempo\w* de carga|lag|optimiza\w+|crash\w*|bug\w*)\b"],
    "ia": [r"\b(ia|inteligencia artificial|la ia|los enemigos (son|parecen) (tontos|listos)|comportamiento (enemigo|de los enemigos))\b"],
    "ritmo": [r"\b(ritmo|pacing|lento\w*|r[aá]pido\w*|se alarga|respir\w+|tedioso|pesado)\b"],
    "dificultad": [r"\b(dificultad|dif[ií]cil|f[aá]cil|injusto|balance\w*|equilibr\w+)\b"],
    "interfaz": [r"\b(interfaz|men[uú]s?|hud|ui|iconos?|marcador\w*)\b"],
    "cámara": [r"\b(c[aá]mara)\b"],
}
_OTHER_GAME = re.compile(
    r"\b(me recuerda (mucho |muchísimo |bastante )?a|como (en|pasaba en|hacía)|igual que (en )?|parecido a|"
    r"al estilo (de )?|en comparación con|comparado con|a diferencia de|lo mismo que (en )?)\s+"
    r"(?P<name>(?:[A-ZÁÉÍÓÚ][\w'’:\-]*|\d+)(?:\s+(?:[A-ZÁÉÍÓÚ][\w'’:\-]*|\d+|de|of|the|el|la|los|las|y|and|:)){0,5})")
_GENERAL = re.compile(r"\b(muchos juegos|la industria|últimamente (los|muchos) juegos|los juegos (de hoy|actuales|modernos)|"
                      r"en general (los|en los) juegos|este género|los (shooters|rpg|souls|roguelikes|metroidvanias)|"
                      r"todos los juegos|cada vez más juegos)\b", re.IGNORECASE)
_PERSONAL = re.compile(r"\b(cuando (era|yo era) (pequeño|niño|chico|joven)|de pequeño|de niño|mi infancia|"
                       r"me acuerdo (de )?cuando|me hace pensar en (cuando|mi)|personalmente|en mi vida)\b", re.IGNORECASE)

_NUM_WORDS = {"uno": 1, "una": 1, "primer": 1, "primero": 1, "primera": 1, "dos": 2, "segundo": 2, "segunda": 2,
              "tres": 3, "tercer": 3, "tercero": 3, "tercera": 3, "cuatro": 4, "cuarto": 4, "cuarta": 4,
              "cinco": 5, "quinto": 5, "quinta": 5, "seis": 6, "sexto": 6, "sexta": 6, "siete": 7, "septimo": 7,
              "séptimo": 7, "ocho": 8, "octavo": 8, "nueve": 9, "noveno": 9, "diez": 10, "decimo": 10, "décimo": 10,
              "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "veinte": 20}
_CHAPTER = re.compile(r"\b(cap[ií]tulo|acto|misi[oó]n|episodio|parte)\s+(?P<n>\d{1,3}|[a-záéíóú]+)\b", re.IGNORECASE)
_SECTION_HEAD = re.compile(
    r"\b(?:entrando|llegando|llegu[eé]|llego|entro|estoy|estamos|acabo de (?:llegar|entrar)|volv[ií])"
    r"(?:\s+[^.;:!?\s]+){0,4}?[\s,]+(?:a|al|en)\s+(?:la\s+|el\s+|una\s+|un\s+)?"
    r"(?P<head>zona|nivel|[aá]rea|secci[oó]n|f[aá]brica|ciudad|base|bosque|cueva|edificio|castillo|pueblo|"
    r"mazmorra|templo|puente|estaci[oó]n|hospital|mansi[oó]n|iglesia|prisi[oó]n|laboratorio|puerto|aldea|"
    r"barrio|calle|t[uú]nel|mina|torre|nave|refugio|campamento)\b(?P<rest>[^.,;:!?]*)", re.IGNORECASE)
_SECTION_STOP = {"y", "o", "que", "donde", "pero", "porque", "con", "hay", "es", "está", "esta", "son", "se", "me",
                 "lo", "le", "les", "ahora", "aquí", "aqui", "ya", "cuando", "como", "para", "sin", "muy"}
_SECTION_LINK = {"de", "del", "la", "las", "los", "el"}


def detect_chapter(text: str) -> str:
    """'Estoy entrando al capítulo tres' → 'Capítulo 3'. Vacío si no se menciona."""
    m = _CHAPTER.search(text)
    if not m:
        return ""
    raw = m.group("n").lower()
    num = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
    if num is None:
        return ""
    return f"{m.group(1).capitalize()} {num}"


def detect_section(text: str) -> str:
    """'Acabo de llegar a la zona del puerto' → 'Zona del puerto'. Vacío si no se menciona un lugar."""
    m = _SECTION_HEAD.search(text)
    if not m:
        return ""
    words = [m.group("head")]
    for w in m.group("rest").split():
        if w.lower() in _SECTION_STOP or len(words) >= 5:
            break
        words.append(w)
    while len(words) > 1 and words[-1].lower() in _SECTION_LINK:   # sin conectores colgando
        words.pop()
    s = " ".join(words)
    return s[:1].upper() + s[1:60]


_COMPILED_F = {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in _FUNC_PATTERNS.items()}
_COMPILED_T = {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in _TOPIC_PATTERNS.items()}


class ContextEngine:
    def __init__(self, known_games: list[str] | None = None) -> None:
        self.known_games = [g for g in (known_games or []) if g]
        self._lock = threading.Lock()
        self._state: dict[str, Any] = self._empty_state("")

    @staticmethod
    def _empty_state(game: str) -> dict[str, Any]:
        return {"game": game, "chapter": "", "section": "", "topic": "", "subtopic": "", "last_function": "",
                "references": [], "related": []}

    def state(self) -> dict:
        with self._lock:
            st = dict(self._state)
            st["references"] = list(st["references"])
            st["related"] = list(st["related"])
            return st

    def reset(self, game: str = "") -> None:
        with self._lock:
            self._state = self._empty_state(game)

    def set_known_games(self, games: list[str]) -> None:
        self.known_games = [g for g in games if g]

    def apply_llm(self, result: dict) -> None:
        """Incorpora al estado de la sesión lo que aportó la IA para la nota más reciente."""
        with self._lock:
            st = self._state
            for key in ("chapter", "section", "subtopic"):
                if result.get(key):
                    st[key] = str(result[key])[:60]
            topics = result.get("topics") or []
            if topics:
                st["topic"] = topics[0]
            if result.get("functions"):
                st["last_function"] = result["functions"][0]
            for r in result.get("refs") or []:
                name = r.get("name") if isinstance(r, dict) else str(r)
                if name and name not in st["references"]:
                    st["references"].append(name)
            st["references"] = st["references"][-8:]

    def classify(self, text: str, game: str = "", recent: list[dict] | None = None) -> dict:
        t = text.strip()
        low = t.lower()
        words = max(1, len(re.findall(r"\w+", low)))

        # ---- función -------------------------------------------------------
        fscores: dict[str, float] = {}
        for name, pats in _COMPILED_F.items():
            hits = sum(len(p.findall(low)) for p in pats)
            if hits:
                fscores[name] = hits
        # heurísticas estructurales: presente descriptivo largo sin valoración → descripción
        if not fscores and words >= 12 and re.search(r"\b(hay|está|están|es|son|tiene|tienen)\b", low):
            fscores["descripción"] = 0.5
        # ---- tema ----------------------------------------------------------
        tscores: dict[str, float] = {}
        for name, pats in _COMPILED_T.items():
            hits = sum(len(p.findall(low)) for p in pats)
            if hits:
                tscores[name] = hits
        # ---- ámbito y referencias ------------------------------------------
        refs: list[dict] = []
        for m in _OTHER_GAME.finditer(t):
            name = m.group("name").strip(" :-")
            if name and name.lower() != (game or "").lower() and len(name) >= 3:
                refs.append({"name": name, "relation": "comparación"})
        for g in self.known_games:
            if g.lower() != (game or "").lower() and re.search(r"\b" + re.escape(g.lower()) + r"\b", low):
                if not any(r["name"].lower() == g.lower() for r in refs):
                    refs.append({"name": g, "relation": "referencia"})
        scope, scope_conf = "current", 0.55
        if _PERSONAL.search(low):
            scope, scope_conf = "personal", 0.7
        elif _GENERAL.search(low):
            scope, scope_conf = "general", 0.7
        elif refs:
            # Si la frase es sobre todo una comparación, la referencia es externa pero el
            # comentario sigue siendo sobre el juego actual ("esto me recuerda a X").
            scope = "current" if "comparación" in fscores else "other"
            scope_conf = 0.6
        elif game and re.search(r"\b" + re.escape(game.lower()) + r"\b", low):
            scope_conf = 0.8
        elif tscores:
            scope_conf = 0.65

        functions = [k for k, _ in sorted(fscores.items(), key=lambda kv: -kv[1])[:2]]
        topics = [k for k, _ in sorted(tscores.items(), key=lambda kv: -kv[1])[:3]]
        if refs and "comparación" not in functions:
            functions = (["comparación"] + functions)[:2]

        # ---- confianza -------------------------------------------------------
        f_conf = min(1.0, 0.35 + 0.2 * sum(fscores.values())) if fscores else 0.2
        t_conf = min(1.0, 0.35 + 0.2 * sum(tscores.values())) if tscores else 0.2
        # continuidad: si no hay tema pero el anterior lo tenía, heredarlo con poca confianza
        inherited = False
        with self._lock:
            prev_topic = self._state.get("topic", "")
        if not topics and prev_topic and words >= 4:
            topics = [prev_topic]
            t_conf = 0.35
            inherited = True
        confidence = round(0.45 * f_conf + 0.35 * t_conf + 0.2 * scope_conf, 2)
        ambiguous = confidence < 0.5 or (not functions and not topics)

        with self._lock:
            st = self._state
            st["game"] = game or st.get("game", "")
            if topics and not inherited:
                if topics[0] != st.get("topic"):
                    st["subtopic"] = topics[1] if len(topics) > 1 else ""
                st["topic"] = topics[0]
            elif topics and len(topics) > 1:
                st["subtopic"] = topics[1]
            if functions:
                st["last_function"] = functions[0]
            for r in refs:
                if r["name"] not in st["references"]:
                    st["references"].append(r["name"])
            st["references"] = st["references"][-8:]
            st["related"] = [x for x in topics[1:]] + ([prev_topic] if prev_topic and prev_topic not in topics else [])
            chapter, section = detect_chapter(t), detect_section(t)
            if chapter and chapter != st.get("chapter"):
                st["chapter"] = chapter
                st["section"] = ""          # capítulo nuevo: la sección anterior ya no aplica
            if section:
                st["section"] = section

        ctx = {"topic_inherited": inherited, "function_scores": fscores, "topic_scores": tscores}
        if chapter:
            ctx["chapter"] = chapter
        if section:
            ctx["section"] = section
        return {"scope": scope, "functions": functions, "topics": topics, "refs": refs, "confidence": confidence,
                "ambiguous": ambiguous, "context": ctx}
