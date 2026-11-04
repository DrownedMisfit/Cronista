# Cronista

Manual completo del teleprompter.

## Teleprompter

Solo necesita la interfaz (`requirements-teleprompter.txt`: PySide6-Essentials). Se abre con `run_cronista.bat`.

- **Texto**: «✎ Editar texto» para escribir o pegar; «📂 Abrir…» (o arrastrar el archivo a la ventana) para
  `.txt`, `.md` (se quitan los símbolos de Markdown) y `.docx` (Word; un Google Docs se descarga como .docx). El
  texto se guarda solo en `%APPDATA%\Cronista\teleprompter.txt`.
- **Lectura**: el texto sube a velocidad constante; los triángulos azules marcan la línea de lectura (a un tercio
  de la altura). Al llegar al final se para; «Iniciar» vuelve a empezar.
- **Banderas rojas**: al pausar (botón o Espacio) queda una bandera roja a la izquierda de la línea donde
  paraste; cada pausa la mueve (no se acumulan). Al volver a abrir Cronista, el texto sigue desde ella.
  Además puedes poner las tuyas: al pasar el ratón por el margen izquierdo aparece una bandera tenue junto a la
  línea; un clic la pone y un clic sobre una bandera la quita. «🚩 Ir a la marca» (o B) va a la siguiente
  bandera y «✕ Quitar marcas» las quita todas. Se guardan como posiciones en el texto, así que siguen en su sitio
  aunque cambies la letra o el tamaño. La de la pausa se quita al terminar el texto; todas, al cambiarlo.
- **🗑 Borrar texto** (junto a «Abrir»): deja el teleprompter vacío para escribir o pegar otro texto (pide
  confirmación; mientras no cierres Cronista, Ctrl+Z en el editor lo recupera).
- **Barra de avance** (a la derecha, se puede ocultar): en azul lo ya leído, punto blanco donde vas y raya roja
  donde está la bandera. Pulsarla o arrastrarla salta a esa parte del texto.
- **Velocidad** 1-30 (10 ≈ un tercio de línea por segundo, ritmo de habla normal) y **tamaño** 20-200 pt. Se
  recuerdan entre usos, igual que el tipo de letra, el espaciado y el espejo.
- **Tipo de letra** (segunda fila de la barra, o L / Mayús+L mientras lees): nueve de las más legibles. La mejor
  letra cambia de una persona a otra (Wallace et al., ACM TOCHI 2022: hasta un 35 % más de velocidad entre la
  mejor y la peor de cada lector), así que conviene probarlas con el texto real. Tres vienen con Cronista en
  `fonts/` y no se instalan en Windows: Atkinson Hyperlegible Next (Braille Institute, baja visión), Luciole
  (baja visión) y Lexend; las otras son de Windows: Verdana, Tahoma, Segoe UI, Arial, Calibri y Sitka. Licencias
  en `fonts/LICENCIAS.md`. No se incluyó OpenDyslexic: en estudios con lectores con dislexia no mejoró la lectura.
- **Espaciado** 0-50 %: separación extra entre letras. Separarlas ayuda a que no se «amontonen» (Zorzi et al.,
  PNAS 2012, con lectores con dislexia).
- **Espejo**: refleja el texto en horizontal para el cristal de un teleprompter (los controles no se reflejan).
- **Centrar líneas**: cada línea centrada a lo ancho en vez de alineada a la izquierda.
- **Lectura al centro**: la línea de lectura pasa de un tercio a media altura (a la altura del objetivo de la
  cámara si usas el cristal). El texto no salta: sigue en la línea de lectura lo mismo que antes.
- **Ángulo de visión** (−40° a +40°): si miras la pantalla desde arriba (o desde abajo), el texto se dibuja en
  trapecio, más ancho por el lado que te queda lejos, para que desde tu posición se vea recto sin mover el
  monitor. Modelo: el ojo a unas 2,5 alturas de pantalla; ajústalo a ojo hasta que lo veas recto. Doble clic en
  el número vuelve a 0°. Solo se deforma el texto (y sus banderas): la banda de lectura, los triángulos y la
  barra de avance se quedan rectos. Con un ángulo distinto de 0 cada fotograma cuesta ~5,6 ms a 1920×1080 (sin ángulo,
  ~0,4 ms). La barra de controles tiene tres filas: reproducción, letra y vista.
- **Pantalla completa**: se usa la pantalla donde esté la ventana (para un monitor del teleprompter, mueve antes
  la ventana allí). La barra y el cursor se ocultan a los 3 s; mover el ratón los muestra.

| Tecla | Acción |
|---|---|
| Espacio | Iniciar / pausar (al pausar deja la bandera roja) |
| B | Ir a la siguiente bandera roja |
| Clic en el margen izquierdo | Poner / quitar una bandera en esa línea |
| Inicio | Volver al principio |
| ↑ / ↓ | Más / menos velocidad |
| RePág / AvPág | Retroceder / avanzar 3 líneas (también la rueda del ratón, de línea en línea) |
| + / − (o Ctrl + rueda) | Letra más grande / más pequeña |
| M | Espejo |
| C | Centrar líneas |
| L / Mayús+L | Siguiente / anterior tipo de letra |
| F11 / Esc | Pantalla completa / salir |
| Ctrl+O / Ctrl+E | Abrir archivo / editar texto |

Consumo: el texto se pinta en una imagen que cada fotograma solo copia (~0,3 ms a 1920×1080) y el temporizador
solo corre mientras se desplaza: en pausa no gasta nada.

**Movimiento continuo y tarjeta gráfica** (v0.7): el texto se coloca con fracciones de píxel. Antes se movía
de píxel entero en píxel entero y, a velocidad normal (~43 px/s), se quedaba quieto varias imágenes y saltaba 1 px
(«sube, pausa, sube»). Si hay OpenGL, dibuja la tarjeta gráfica (`PrompterViewGL`): el texto maquetado se sube como
textura, se coloca con fracciones de píxel sin coste y se presenta sincronizado con el monitor; el procesador baja
de ~40 % a ~6-10 % de un núcleo, también con ángulo de visión. Sin OpenGL (o con `use_gpu: false` en
settings.json) dibuja el procesador (`PrompterView`), también con fracciones de píxel pero gastando más.

**Fluidez** (fila 2): Automático, 60, 120, 144 o 240 fps; al lado se ve la cifra que se está usando. Automático
usa los Hz del monitor donde está la ventana, pero baja si pintar ocupa más de la mitad de cada fotograma o si el
programa pasaría de un núcleo de CPU (por eso sirve en equipos modestos). Medido a 1920×1080: sin ángulo, 240 fps
reales con ~25 % de un núcleo; con ángulo de visión, el automático baja a ~65 fps (~1 núcleo).

Código: `cronista/teleprompter/` (`text.py` sin Qt, `fonts.py` la lista de letras, `view.py` la superficie de
lectura, `window.py` los controles). Pruebas: `tests/test_teleprompter.py`.

---

# Asistente de notas por voz (en pausa)


Asistente de notas por voz para sesiones de videojuegos, sincronizado con OBS Studio.
Escucha tu micrófono de forma independiente (no toca el audio de OBS), detecta cuándo hablas,
transcribe localmente y guarda cada comentario con el timestamp exacto de la grabación.

Todo funciona en local. Solo las funciones marcadas **(online)** usan internet, y solo cuando las activas.

## Requisitos

- Windows 11, Python 3.12 (el entorno `.venv` del proyecto ya tiene todo instalado).
- OBS Studio 28 o superior con el **servidor WebSocket activado**:
  OBS → Herramientas → Ajustes del servidor WebSocket → "Activar servidor WebSocket".
  Cronista lee el puerto y la contraseña de la configuración local de OBS automáticamente.
  También puedes activarlo desde Cronista (Configuración → OBS) con OBS cerrado.

## Arrancar

```bash
run_cronista.bat --notas
```

o, con consola (para ver el registro):

```bash
.venv\Scripts\python.exe -m cronista --notas
```

Opciones: `--minimized` (arranca en la bandeja), `--no-gui` (modo consola), `--debug`. Sin `--notas` (ni
`--minimized`/`--no-gui`) se abre el teleprompter.

La primera vez que termines una frase, se descargará el modelo de transcripción (internet; una sola vez).
También puedes descargarlo antes desde Configuración → Transcripción o con:

```bash
.venv\Scripts\python.exe tools\download_models.py large-v3-turbo
```

## Cómo se usa

Cronista solo se ejecuta cuando lo abres tú (acceso directo o `run_cronista.bat`); no se inicia solo al abrir
un juego ni con Windows (salvo que actives esa opción). Cerrar la ventana cierra la aplicación por completo:
micrófono, hotkey y conexión con OBS se liberan. Si prefieres que al cerrar siga en la bandeja, actívalo en
Configuración → General.

Cronista no arranca con Windows ni vigila OBS en segundo plano: no hay ningún proceso suyo corriendo hasta
que lo abres tú.

1. Abre Cronista antes de empezar a grabar.
2. Pulsa grabar en OBS → Cronista inicia una sesión y abre el micrófono.
3. Habla con naturalidad. Cada frase se transcribe y se guarda con su `[hh:mm:ss]` de la grabación.
4. Mantén pulsado **F8** (configurable) para forzar una nota aunque el detector no la haya abierto.
5. Detén la grabación en OBS → la sesión se cierra con la ruta del archivo y la duración.
6. Revisa, marca (⭐🔥🎬📝❌👍), corrige la clasificación, exporta (TXT/MD/CSV/JSON/SRT)
   o prepara un paquete para ChatGPT/Claude (Sesión → Enviar a chatbot).

Modos: **Híbrido** (recomendado: detección automática + F8), **Automático**, **Manual** (solo F8).

Por defecto el micrófono solo se abre mientras hay una sesión. Puedes cambiarlo en Configuración → General.

## IA local (fase 2)

Cronista usa un modelo de lenguaje **local** para entender lo que dices: llama.cpp (build oficial Vulkan, usa tu GPU
AMD) con **Qwen3.5-4B** cuantizado (Q4_K_M, ~2,7 GB, licencia Apache-2.0). Nada sale de tu equipo.

Instalación (una vez, requiere internet, ~2,8 GB): Configuración → IA local → "Descargar motor y modelo", o

```bash
.venv\Scripts\python.exe tools\download_llm.py
```

Qué hace:

- **Clasificación en dos dimensiones**: función (descripción, análisis, opinión, crítica, comparación…) y tema
  (combate, diseño de niveles…), más ámbito (juego actual / otro juego / tema general / personal), referencias,
  subtema, capítulo y sección, con confianza y "contexto ambiguo". La clasificación rápida heurística aparece al
  instante; la IA la refina después (✨ en la lista).
- **Tus correcciones mandan**: lo que corriges o confirmas queda bloqueado (🔒) y la IA no lo vuelve a tocar.
  Con "Aprender de mis correcciones" activado, tus correcciones se usan como ejemplos para clasificar notas parecidas.
- **Versiones de texto** por nota: Original, Corregida, Mejor expresada e Idea para guion (sin cambiar tu opinión).
- **Comentarios relacionados** (similitud de texto + tema + cercanía en el tiempo, sin IA) y grupos de temas.
- **Informe y análisis de sesión**: distribución por tema, función y ámbito; resumen de críticas, aspectos
  positivos, temas recurrentes, ideas para video y comentarios por desarrollar. Los timestamps que el modelo
  invente se descartan automáticamente: solo quedan los de tus notas reales.

Rendimiento mientras juegas (Configuración → IA local → "Mientras juegas"):

- **Esperar a que no esté jugando** (recomendado): con un juego a pantalla completa, la IA no trabaja y el modelo se
  descarga de la VRAM. Los análisis pendientes se hacen al salir del juego o al terminar la sesión.
- **Solo CPU**: trabaja durante el juego sin tocar la GPU.
- **Sin restricciones**.

El servidor de IA se abre solo cuando hay trabajo, se cierra tras unos minutos sin uso y muere con Cronista.

## Perfil de voz (opcional)

Configuración → Micrófono → "Configurar mi voz…": lees un texto unos 30-45 s y Cronista crea una huella local de
tu voz (un vector de 256 números) con el modelo WeSpeaker ResNet34-LM (26,5 MB, Apache-2.0, en CPU). La grabación
no se guarda; solo la huella, en `%APPDATA%\Cronista\voice\profile.json`, y se puede borrar en cualquier momento.

Con "Usar identificación de voz" activado, cada frase se compara con tu huella antes de transcribirse (~20-60 ms):

- **Es tu voz** → se procesa normal.
- **No parece tu voz** → por defecto se transcribe igualmente y se marca 🗣️ "otra voz" (puedes filtrarlas o
  confirmar "Es mi voz"). Opcionalmente se puede descartar sin transcribir.
- **Hotkey F8 y frases de menos de 1 s** → nunca se filtran: ante la duda, se conserva lo que dices.

El umbral se calibra con tu propia grabación (entre 0,25 y 0,45, deliberadamente permisivo); también puede fijarse a mano.

## Transcripción: rápida mientras juegas, precisa después

- **En vivo: Parakeet-TDT-0.6B-v3** (NVIDIA, CC-BY-4.0) sobre sherpa-onnx (Apache-2.0), en CPU. Medido en tu
  Ryzen 7 5700X: ~0,1 s de cálculo por segundo hablado (Whisper large-v3-turbo: 1,3-2 s, más lento que el tiempo
  real porque procesa siempre ventanas de 30 s). Menos error que Whisper large-v3 en español (MLS 3,41 vs 4,32).
- **Segunda pasada: Whisper + glosario** cuando no hay un juego en primer plano. Repasa lo dicho y solo cambia el
  texto si se parece razonablemente al original y no lo editaste tú. El audio de cada frase se guarda en
  `%APPDATA%\Cronista\audio_pendiente` solo hasta repasarla. Se puede desactivar (Configuración → Transcripción).
- Instalación del modelo en vivo (~670 MB, una vez): Configuración → Transcripción → "Descargar Parakeet".

## Español con títulos en inglés

Si mezclas idiomas («estoy jugando *Rise of Iron: D-Day* y el *gameplay* del *boss*…»), Cronista lo entiende con
un **glosario bilingüe** (Configuración → Transcripción): el juego de la sesión, los juegos que ya registraste, tus
términos y una lista de términos de videojuegos se dan a Whisper como pistas, y después un corrector cambia las
variantes mal entendidas («Rise of Iron Di-Day», «Rise of Iron di de») por la grafía correcta. Puedes añadir
correcciones propias: `Rise of Iron: D-Day = Rise of Iron Di Day, Rise of Iron di de`. Medido con large-v3-turbo y
frases mezcladas: 5/5 correctas (antes 3/5).

## Documentos: Word, PDF y Google Docs

Las notas de una sesión, los guiones, los análisis de sesión y las respuestas del chatbot se pueden guardar como
**Word (.docx)**, **PDF**, **OpenDocument (.odt)**, HTML o Markdown («Guardar como documento…» o Exportar). Para
Google Docs: sube el .docx (o .odt) a Google Drive y ábrelo con Documentos de Google.

## Del juego al video (fase 3)

Todo se lanza desde el menú **Sesión** y corre en segundo plano con una barra de progreso (una tarea a la vez,
prioridad baja). Nada de esto se ejecuta mientras juegas salvo que lo pidas.

- **Enviar a Reclaimer**: crea un proyecto `.eia` de tu editor usando su propio modelo de datos (se lee de
  `F:\Reclaimer`, sin modificarlo): la grabación de OBS en V1 con su audio vinculado, un marcador por nota en su
  segundo exacto (color según ⭐🔥🎬📝❌👍), cada nota como *momento favorito* listo para insertar, y los momentos
  de acción detectados. Opcionalmente abre Reclaimer con el proyecto. Nunca sobrescribe un proyecto existente.
- **Clips de notas**: recorta de la grabación el tramo de cada nota (5 s antes, 3 s después, configurable) y deja el
  vínculo nota↔clip. Modo rápido (copia sin recodificar) o exacto (recodifica con la GPU AMD, `h264_amf`).
- **Marcadores EDL** (DaVinci Resolve) y **marcas de tiempo** para YouTube en Exportar.
- **Buscar en todas las notas** (Ctrl+Mayús+F): búsqueda por significado y por palabras en todas tus sesiones, con
  filtros por juego, ámbito, función, tema y opinión; **comparar entre juegos** qué dijiste de lo mismo; **notas
  parecidas**. Modelo local paraphrase-multilingual-MiniLM (118 MB, Apache-2.0, CPU). Ojo: entiende de qué hablas,
  pero no distingue por sí solo si es bueno o malo: para eso están los filtros de opinión.
- **Generar guion**: esqueleto de video (títulos, gancho, idea central, secciones con puntos, imágenes sugeridas,
  conclusión y preguntas abiertas) con las notas de una o varias sesiones de un juego. Cada punto cita los momentos
  reales en que lo dijiste; las citas inventadas se eliminan y lo que falta se te pregunta en vez de inventarlo.
  Enfoques: análisis, crítica, comparación, ensayo, disonancia ludonarrativa.
- **Momentos destacados**: combina lo que dijiste (marcadores, críticas, grupos de comentarios) con los picos de
  imagen y sonido de la grabación (solo fotogramas clave: ~1 min de análisis por cada 3 h de video, ~7 % de CPU).

## Arquitectura (módulos sustituibles, comunicados por un bus de eventos)

```
cronista/
  core/        eventos, configuración, rutas, hardware, inicio automático, reproductor
  audio/       captura (PortAudio/WASAPI) → ganancia → RNNoise + filtro estacionario → 16 kHz
               → Silero VAD → segmentador (pre-roll, pausas, hotkey) → prueba de micrófono
  stt/         faster-whisper (CTranslate2, int8 en CPU; CUDA si existe); parciales + final; descarga en reposo
  obs/         obs-websocket 5: eventos de grabación, estado periódico, nombre del juego
  session/     reloj de grabación (anclas, pausas, resincronización) y gestor de sesiones
  notes/       base de datos SQLite, gestor de notas y correlación entre comentarios (relations)
  context/     motor de contexto heurístico instantáneo (función / tema / ámbito / referencias / capítulo)
  ai/          IA local: llama-server (local_llm), tareas y prompts (tasks), trabajador con cola y política
               de juego (worker), instalación (llm_setup); proveedores de API opcionales (providers)
  learning/    personalización local: ejemplos de tus correcciones y vocabulario propio
  voice/       perfil de voz: fbank compatible con Kaldi (numpy), WeSpeaker ONNX y puerta antes del STT
  editor/      puente con Reclaimer (genera .eia con sus propias clases de datos)
  clips/       recorte de clips por nota con ffmpeg
  search/      búsqueda semántica (MiniLM ONNX) + palabras (TF-IDF)
  moments/     detección de momentos (notas + fotogramas clave + volumen de la grabación)
  jobs.py      tareas largas en segundo plano con progreso y cancelación
  hotkey/      hotkey global por hook de bajo nivel (funciona en pantalla completa)
  perf/        detección de juego en primer plano y prioridad de proceso
  export/      TXT, Markdown, CSV, JSON, SRT y paquete para chatbot
  ui/          PySide6: ventana principal, docks, bandeja, configuración, diálogos
```

Consumo en silencio: VAD de ~1 ms por bloque de 32 ms + RNNoise. Whisper solo corre al terminar una frase y
se descarga de memoria tras N minutos sin uso. Con "Priorizar rendimiento del juego", el proceso baja de
prioridad y la transcripción se simplifica mientras un juego está a pantalla completa.

## Pruebas

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

## Datos

`%APPDATA%\Cronista\` → `cronista.db`, `settings.json`, `logs\`, `exports\`.
Modelos en `models\` dentro del proyecto.
