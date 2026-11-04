<h1 align="center">Cronista</h1>

<p align="center">
Teleprompter gratuito para Windows, pensado para grabar videos leyendo tu guion.<br>
Movimiento suave, letras muy legibles y banderas para retomar donde te quedaste.
</p>

<p align="center"><a href="https://ko-fi.com/pigmeofurtivoyt"><img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Apóyame en Ko-fi"></a><br><sub>Es gratis. Si te sirve, puedes invitarme un café ☕</sub></p>

---

## Qué hace

- **Desplazamiento suave** a velocidad constante (usa la tarjeta gráfica si hay OpenGL; casi no gasta CPU).
- **Abre tu guion** en `.txt`, `.md` o `.docx` (Word o Google Docs descargado), o pégalo directamente.
- **Banderas rojas**: al pausar queda una marca donde paraste y la próxima vez sigue desde ahí. Puedes poner las
  tuyas con un clic en el margen.
- **Letras pensadas para leer**: nueve tipos (Atkinson Hyperlegible, Lexend…), tamaño y espaciado ajustables.
- **Espejo** para el cristal de un teleprompter, **ángulo de visión** si miras la pantalla desde arriba o abajo,
  pantalla completa en el monitor que elijas y barra de avance.

| Tecla | Acción |
|---|---|
| Espacio | Iniciar / pausar |
| ↑ / ↓ | Más / menos velocidad |
| + / − | Letra más grande / más pequeña |
| B | Ir a la siguiente bandera |
| M | Espejo |
| F11 / Esc | Pantalla completa / salir |

## Instalación (Windows 10 / 11)

**Lo más fácil:** descarga `Cronista.zip` de la sección **[Releases](../../releases)**, descomprímelo y haz doble
clic en **«Abrir Cronista.bat»**. La primera vez prepara todo solo (si hace falta, instala Python).

**Desde el código:**
```
python -m venv .venv
.venv\Scripts\pip install -r requirements-teleprompter.txt
run_cronista.bat
```

El manual completo está en **[MANUAL.md](MANUAL.md)**.

## Licencia

Cronista es software libre bajo la **[GNU GPL v3](LICENSE)**. Las letras incluidas en `fonts/` tienen su propia
licencia libre (SIL Open Font License 1.1 y, Luciole, CC BY 4.0); ver [fonts/LICENCIAS.md](fonts/LICENCIAS.md).
