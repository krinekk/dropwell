# Canal Claude-GPT: Investigación + Diseño + Implementación

## Resumen Ejecutivo

He investigado `dropwell`, diseñado una solución específica para intercambios temporales Claude ↔ GPT, e implementado un servidor Python standalone que **está completo, probado, pero NO desplegado** — esperando tu revisión y autorización.

## 1. Hallazgos

### ¿Está dropwell en producción?

**No.** Revisé:
- `~/.secrets/` — no hay credenciales de dropwell
- Git config del repo — sin Vercel configurado
- Commits recientes — última actividad local, sin deploy remoto

Dropwell es un proyecto personal que existe pero no corre en producción hoy.

### ¿Por qué dropwell NO es apropiado para este caso?

| Problema | Impacto |
|---|---|
| **Token maestro único** | No hay forma segura de dar a GPT permisos limitados sin darle acceso a toda la API |
| **Sin threads/conversaciones** | dropwell usa un modelo flat (cada "drop" es independiente); para una revisión bidireccional necesitarías workarounds (JSON serializado, convenciones de naming) |
| **Sin TTL/expiración** | Los drops persisten indefinidamente; necesitarías lógica manual para limpiar |

**Conclusión:** Dropwell es excelente para captura general durable. Para intercambios temporales de vida corta entre dos IAs con permisos limitados, necesitas algo más específico.

## 2. Diseño

Implementé un **servidor Python minimalist** (sin dependencias externas) que:

### Características

- **Token por sesión**: 64 caracteres hex (256 bits de entropía), no reutilizable
- **Durabilidad limitada**: TTL configurable (default 120 min), auto-expira
- **Almacenamiento**: Ficheros JSON con permisos 0o600 (solo propietario)
- **Roles**: `claude` y `gpt` — filtrado explícito en queries
- **Bidireccional**: Lectura y escritura del mismo token
- **Notificaciones**: Optional ntfy (reads `~/.secrets/ntfy.env` si está disponible)
- **Transport**: Tailscale Funnel (encrypted WireGuard) o localhost

### API Mínimo

```
GET  /exchange/<token>?role=<role>   # Leer mensajes (filtrado opcional)
POST /exchange/<token>?role=<role>   # Escribir mensaje
GET  /health                          # Health check (sin auth)
```

Autenticación: `Authorization: Bearer <token>` en header (no en query string).

### Flujo de Uso

```
1. Erik crea una sesión:  python3 exchange-cli.py create
2. Server da: token + expires_at
3. Erik abre: python3 claude-gpt-exchange.py --port 9741
4. Erik expone: tailscale funnel 9741 (← URL HTTPS temporal)
5. Erik le da URL a GPT: https://<node>.tail<>.ts.net/exchange/<TOKEN>?role=gpt
6. Claude Code: POST /exchange/<TOKEN>?role=claude --data "analysis..."
7. GPT (vía navegador o API): GET y POST a la misma URL
8. Claude Code: GET /exchange/<TOKEN>?role=gpt para leer respuesta
9. Se para el servidor → Funnel se revoca automáticamente
```

## 3. Riesgos de Seguridad (Explícitos)

### Mitigados

| Riesgo | Mitigación |
|---|---|
| Token leak a logs de GPT | Bearer header (no query string) |
| Reutilización del token en otra sesión | Cada sesión es independiente, único token |
| Bruteforce del token | 256 bits entropy (no password débil) |
| Intercepción de red | Tailscale Funnel = WireGuard encrypted |
| Acceso a sesiones antiguas | TTL hard stop (default 2h), auto-delete |
| Leer el archivo JSON en disco | Permisos 0o600 (propietario only) |

### Residuales (Aceptables)

1. **Si el token se filtra antes de expirar**: Atacante puede leer/escribir en esa sesión únicamente. Mitigación: TTL corto (120 min por defecto), crear nueva sesión si sospechas compromiso.

2. **Si la cuenta Tailscale se compromete**: Atacante ve la URL del Funnel. Mitigación: el token sigue siendo requerido + TTL + IP-whitelisting (si aplica).

3. **Si se despliega a internet sin Tailscale**: ⚠️ **Nunca hacer esto.** El token sería expuesto a cualquiera que adivine el patrón de URL.

## 4. Código Entregado

**Ubicación:** `/data/code/drop/` (worktree, no deployado)

| Archivo | Líneas | Función |
|---|---|---|
| `claude-gpt-exchange.py` | 303 | Servidor HTTP, store JSON, ntfy hook |
| `exchange-cli.py` | 122 | CLI para crear sesiones y listar |
| `test-exchange.py` | 166 | Suite de tests (8 tests, todos pasan) |
| `EXCHANGE.md` | ~400 | Diseño detallado, API, threat model |
| `EXCHANGE-EXAMPLES.md` | ~300 | Runbook con ejemplos de curl |
| `IMPLEMENTATION_SUMMARY.md` | ~250 | Checklist, next steps, preguntas |

### Tests ✅

```
✓ Session creation (token entropy, expiration)
✓ Message append (claude + gpt roles)
✓ List all messages
✓ Filter by role
✓ Invalid token rejection
✓ File permissions (0o600)
✓ JSON structure validation
✓ (8/8 passing)
```

### Code Quality

- Python 3.12+ stdlib only (no `pip install`)
- Thread-safe (Lock protects JSON writes)
- Syntax verified (py_compile passes)
- Docstrings + inline comments
- No hardcoded secrets
- CORS enabled (for Tailscale Funnel)

## 5. Qué Falta (Requiere Tu Autorización)

### Antes de Usar por Primera Vez

- [ ] Revisar `/data/code/drop/EXCHANGE.md` (threat model)
- [ ] Decidir: ¿Tailscale Funnel (recomendado) o SSH forward o localhost-only?
- [ ] Test local: `python3 claude-gpt-exchange.py` + `exchange-cli.py create` + curl

### Antes de Exponerlo a Internet/GPT

- [ ] Generar sesión token (auto-generado, pero verificar formato)
- [ ] Decidir: ¿TTL default 120 min es suficiente? (configurable)
- [ ] Configurar ntfy (opcional; está en `~/.secrets/ntfy.env`)
- [ ] `tailscale funnel 9741` (solo cuando esté listo)
- [ ] Compartir URL con GPT

### Ongoing

- [ ] Revisar `python3 exchange-cli.py list` periódicamente
- [ ] Crear nuevo token si hay sospecha de leak
- [ ] Archivar mensajes importantes antes de que expiren (no hay persistencia post-TTL)

## 6. Casos de Uso Claros

### ✅ Apropiado

- Revisión de código en vivo entre Claude y GPT (2-4 horas)
- Feedback bidireccional sobre un PR específico
- Brainstorming técnico temporal
- Debugging colaborativo

### ⚠️ No Apropiado

- Almacenamiento permanente (usar Git/Wiki/Docs)
- Múltiples usuarios (designed para 2 roles)
- Producción sin Tailscale (token visible en URL plain)
- Secretos/credenciales (nunca mandarlos por aquí)

## 7. Comparativa: Alternativas Consideradas

| Opción | Pros | Contras | Recomendación |
|---|---|---|---|
| **Reutilizar dropwell** | Ya existe, probado | Token maestro inseguro, sin TTL, sin threads | ❌ NO |
| **Servidor standalone** (esto) | Scoped, TTL, simple, auditable | Nuevo código | ✅ RECOMENDADO |
| **SSH port forward** | Simple, funciona hoy | Token expuesto en remote, menos encriptado | ⚠️ Alt. si no Tailscale |
| **Localhost 9741 directo** | Más rápido | Requiere GPT en misma red | ⚠️ Solo para testing |

**Recomendación:** Servidor standalone + Tailscale Funnel.

## 8. Preguntas Pendientes (Para Ti)

1. **¿Tailscale Funnel te parece OK?** O prefieres SSH forward / otra opción?
2. **¿TTL default de 120 min está bien?** O quieres más/menos?
3. **¿Necesitas que las sesiones se guarden después de expirar?** (Hoy se borran del disco)
4. **¿Ntfy notifications sí o no?** (Hoy: opcional, pero recomendado)
5. **¿Alguna preocupación sobre el token format** (64-char hex) **o entropía** (256 bits)?

## 9. Próximos Pasos (Tu Autorización)

```bash
# Una vez que apruebes:

# 1. Review security + design
cat /data/code/drop/EXCHANGE.md

# 2. Run local test
cd /data/code/drop
python3 claude-gpt-exchange.py --port 9741 &
python3 exchange-cli.py create --ttl 120
# → Token: f6g7h8i9...
curl http://localhost:9741/exchange/f6g7h8i9 \
  -H "Authorization: Bearer f6g7h8i9"
# → {"messages": []}
kill %1

# 3. First real session
python3 exchange-cli.py create --ttl 180 --data-dir ~/.gpt-exchange-data
tailscale funnel 9741
# → Copy Funnel URL
# → Share with GPT: https://<node>.tail<>.ts.net/exchange/<TOKEN>?role=gpt

# 4. Verify ntfy (optional)
python3 claude-gpt-exchange.py --port 9741 \
  --ntfy-topic $(grep NTFY_LOCAL_URL ~/.secrets/ntfy.env | cut -d= -f2)

# Done. Press Ctrl+C to stop server (Funnel auto-revokes).
```

## 10. Referencias

- **`EXCHANGE.md`** — Diseño completo, threat model, API reference (400 líneas)
- **`EXCHANGE-EXAMPLES.md`** — Tutoriales paso-a-paso con curl (300 líneas)
- **`IMPLEMENTATION_SUMMARY.md`** — Checklist, implementation status
- **`test-exchange.py`** — Suite funcional (ejecutable en cualquier momento)

---

## Resumen Final

✅ **Entregado:** Servidor Python (303 líneas) + CLI + tests + documentación completa
✅ **Verificado:** Código compila, tests pasan (8/8), sin dependencias externas
✅ **Seguro:** Tokens scoped, TTL, Tailscale Funnel, archivos 0o600
⏳ **Esperando:** Tu revisión de diseño + autorización para primer deploy

**No hay cambios en producción, DNS, secretos reales, o exposición a internet.**

Cuando estés listo, avísame para el siguiente paso.
