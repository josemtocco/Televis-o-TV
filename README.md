# Televisão.TV → M3U para SS IPTV / players compatíveis

**Versão 7**

Gera automaticamente uma playlist `.m3u` a partir de [televisao.tv](https://televisao.tv/), organizada por categorias (Notícias, Esporte, Filmes, Regionais, etc.).

Ideal para **SS IPTV**, TiviMate, VLC, IPTV Smarters e players que aceitam M3U com `group-title`.

---

## O que mudou na v7

| Área | Melhoria |
|------|----------|
| Config | Categorias, timeouts e concorrência em `config.json` |
| Código | Tipagem, CLI (`--dry-run`, `--concurrency`), estrutura mais clara |
| Streams | Extração de JSON embutido + priorização (m3u8 > mpd > m3u) |
| Browser | Headers e contexto mais realistas; limite de iframes |
| Status | `status.json` com versão, timestamp UTC e métricas |
| CI | Cache do Playwright, artefatos e diagnóstico mais completo |
| Docs | README completo com uso local e no GitHub Actions |

Mantém as correções da v6:

- só links `a:visible` (evita o catálogo global ~370 canais)
- não espera `networkidle` nas páginas de canal
- sem HEAD/GET longo por stream
- até 20 canais em paralelo (configurável)

---

## Uso local

### Requisitos

- Python 3.11+
- Playwright (Chromium)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

### Gerar a lista

```bash
python gerar_m3u.py
```

Opções:

```bash
python gerar_m3u.py --config config.json
python gerar_m3u.py --concurrency 12
python gerar_m3u.py --dry-run          # só descobre canais, sem streams
```

Arquivos gerados:

- `canais.m3u` — playlist
- `status.json` — métricas, erros e detalhes por canal

---

## Configuração (`config.json`)

Principais campos:

| Campo | Descrição |
|-------|-----------|
| `categories` | Lista de `{ "name", "path" }` |
| `concurrency` | Páginas de canal em paralelo (padrão 20) |
| `max_streams_per_channel` | Máximo de URLs por canal (padrão 8) |
| `player_wait_ms` | Tempo de espera após carregar a página do canal |
| `blocked_slugs` | Slugs que não são canais |

Edite as categorias ou timeouts sem alterar o código.

---

## GitHub Actions

O workflow `.github/workflows/atualizar.yml`:

- Roda a cada **6 horas** e sob demanda (`workflow_dispatch`)
- Instala Playwright com cache
- Gera a M3U e faz commit de `canais.m3u` + `status.json` se houver streams
- Publica artefatos para diagnóstico

Para usar:

1. Faça fork do repositório
2. Ative Actions no repositório
3. (Opcional) Ajuste o cron no YAML

A playlist raw ficará em:

```text
https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPO/main/canais.m3u
```

---

## Formato da M3U

```text
#EXTM3U
#EXTINF:-1 tvg-name="Canal X" tvg-logo="https://..." group-title="Notícias",Canal X
https://.../playlist.m3u8
```

- `group-title` = categoria
- Um canal pode aparecer em mais de um grupo se estiver em várias categorias

---

## Limitações

- Streams dependem do que o site expõe no HTML/rede/player no momento da coleta
- Alguns canais usam players protegidos ou tokens temporários — podem falhar
- O site pode mudar layout; se a descoberta cair, ajuste seletores ou `config.json`
- Não valida se o stream está online (propositalmente rápido)

---

## Licença / uso

Projeto de uso pessoal/educacional. Respeite os termos de uso de televisao.tv e a legislação local sobre retransmissão de conteúdo.
