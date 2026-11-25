<h1 align="center">Cronista</h1>

<p align="center">
Teleprompter gratuito para Windows, pensado para grabar videos leyendo tu guion.<br>
Movimiento suave, letras muy legibles y banderas para retomar donde te quedaste.
</p>

<p align="center"><a href="https://github.com/DrownedMisfit/Cronista/releases/latest/download/Cronista.zip"><img src="https://img.shields.io/badge/%E2%AC%87%20Descargar-Windows%2010%20%2F%2011-orange?style=for-the-badge" alt="Descargar para Windows"></a><br><sub>Gratis · <a href="#-descargar-y-usar-windows-10--11">cómo instalarlo</a></sub></p>

<p align="center"><a href="https://ko-fi.com/pigmeofurtivoyt"><img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Apóyame en Ko-fi"></a><br><sub>Es gratis. Si te sirve, puedes invitarme un café ☕</sub></p>

---

## Qué hace

- **Desplazamiento suave** a velocidad constante (usa la tarjeta gráfica si hay OpenGL; casi no gasta CPU).
- **Abre tu guion** en `.txt`, `.md` o `.docx` (Word o Google Docs descargado), o pégalo directamente.
- **Banderas rojas**: al pausar queda una marca donde paraste y la próxima vez sigue desde ahí. Puedes poner las
  tuyas con un clic en el margen.
- **🎙 Modo Smart**: el texto sigue tu voz (reconocimiento local con Vosk, sin internet al leer): si te paras, se
  para contigo. Entiende mejor tu guion porque escucha solo sus palabras, y también los nombres raros, las siglas y
  los números (si alguna palabra no la puede oír, sale subrayada). El modelo de voz (~40 MB) se baja la primera vez.
- **↺ Toma mala** (R): vuelve al principio de la frase y sigue tras una cuenta atrás corta, sin ventanas.
- **⚠ Revisar lectura**: con Smart, apunta en silencio las frases donde parece que te comiste algo y te las enseña
  al pausar. Nunca te interrumpe; se puede apagar.
- **✂ Líneas por frases**: corta el guion donde una persona haría una pausa (análisis gramatical local con spaCy).
- **⏱ Cuenta atrás** 3-2-1 y tiempo (llevas / dura / faltan), y **📷 Junto a la cámara**: una franja siempre encima,
  cerca de la webcam.
- **Letras pensadas para leer**: 19 tipos (Atkinson Hyperlegible, Lexend, Andika, B612…), tamaño y espaciado ajustables.
- **Espejo** para el cristal de un teleprompter, **ángulo de visión** si miras la pantalla desde arriba o abajo,
  pantalla completa en el monitor que elijas y barra de avance.
- **Interfaz limpia**: una sola barra abajo con lo de leer y un panel de ajustes a la derecha que abres y cierras
  con la tecla A.

| Tecla | Acción |
|---|---|
| Espacio | Iniciar / pausar |
| ↑ / ↓ | Más / menos velocidad |
| + / − | Letra más grande / más pequeña |
| B | Ir a la siguiente bandera |
| F | Líneas por frases |
| K | Junto a la cámara |
| L / Mayús+L | Siguiente / anterior tipo de letra |
| M | Espejo |
| R | Toma mala: repetir la frase |
| A | Abrir / cerrar los ajustes |
| F11 / Esc | Pantalla completa / salir |

## ⬇ Descargar y usar (Windows 10 / 11)

<p align="center"><a href="https://github.com/DrownedMisfit/Cronista/releases/latest/download/Cronista.zip"><b>⬇ Descargar Cronista (2 MB)</b></a></p>

1. Descomprime **Cronista.zip**: clic derecho → **Extraer todo** (no lo abras desde dentro del zip).
2. Entra en la carpeta y haz doble clic en **«Abrir Cronista.bat»**. La primera vez prepara todo solo (unos 200 MB,
   necesita internet; si no tienes Python, lo instala). Después se abre directamente.
3. Si Windows dice **«Windows protegió su PC»**: **«Más información» → «Ejecutar de todas formas»**.

En la carpeta viene **«COMO USAR.txt»** con lo básico. Todas las versiones:
[página de descargas](https://github.com/DrownedMisfit/Cronista/releases).

<details>
<summary>Para programadores: desde el código</summary>

```
python -m venv .venv
.venv\Scripts\pip install -r requirements-teleprompter.txt
run_cronista.bat
```
</details>

El manual completo está en **[MANUAL.md](MANUAL.md)**.

## Licencia

Cronista es software libre bajo la **[GNU GPL v3](LICENSE)**. Las letras incluidas en `fonts/` tienen su propia
licencia libre (SIL Open Font License 1.1 y, Luciole, CC BY 4.0); ver [fonts/LICENCIAS.md](fonts/LICENCIAS.md).
La letra de la interfaz (Inter, OFL 1.1) y los iconos (Lucide, ISC) van en `cronista/teleprompter/assets/` con sus
licencias.
