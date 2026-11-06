"""Shared operator help shown by `baton help` and `baton: help`."""

HELP_TEXT = """\
BATON — ajuda rápida

Como funciona
  Um vendor assume a tarefa inteira e continua nela até concluí-la ou atingir o limite
  configurado. Depois disso, Baton passa o histórico completo e os IDs das sessões anteriores
  ao próximo vendor disponível. `phases = true` ativa o fluxo opcional plan/build/review do Baton.

Comandos no terminal
  baton help
      Mostra esta ajuda.
  baton doctor [goal.toml]
      Verifica CLIs, autenticação, limites e configuração da Claude API.
  baton run goal.toml
      Executa uma tarefa fora do OpenCode.
  baton serve [--port 18777]
      Inicia o gateway local usado pelo OpenCode.
  baton opencode-config [--port 18777]
      Imprime a configuração do provider `baton/auto`.
  baton jobs
      Lista jobs e goals conhecidos.
  baton status <job|goal.toml>
      Mostra o estado e os últimos eventos.
  baton say <job|goal.toml> [--now] <mensagem>
      Envia uma instrução para a tarefa atual.
  baton goal <job|goal.toml> <texto>
      Fixa um objetivo que será incluído nos próximos prompts.
  baton goal <job|goal.toml> --off
      Remove o objetivo fixado.
  baton next <job|goal.toml> [--now]
      Pede o próximo vendor da fila. `--now` troca depois da chamada de ferramenta atual.
  baton use <job|goal.toml> <codex|antigravity|claude> [--now]
      Seleciona um vendor específico; `claude` usa a Claude API. Sem `--now`, a troca espera o
      fim da rodada atual. Um vendor indisponível não é substituído silenciosamente por outro.
  baton stop <job|goal.toml> [--now]
      Para o job após a rodada atual, ou depois da chamada de ferramenta atual com `--now`.
  baton restart [--now]
      Reinicia o serviço e retoma os jobs.
  baton install-service [--port 18777]
      Instala o serviço do gateway no launchd (macOS).

Comandos dentro do OpenCode
  baton: help
      Mostra esta ajuda na conversa.
  baton: goal <texto>
  baton: goal off
  baton: next
  baton: next now
  baton: use codex
  baton: use antigravity now
  baton: use claude
  baton: use claude now

Usar a Claude API diretamente
  1. Crie o diretório e salve sua chave da Anthropic Console:
       mkdir -p ~/.config/baton
       ( umask 077; cat > ~/.config/baton/anthropic.key )  # cole a chave e pressione Ctrl-D
  2. Restrinja a leitura do arquivo: chmod 600 ~/.config/baton/anthropic.key
  3. Verifique a configuração: baton doctor
  4. Selecione a Claude API na conversa: baton: use claude
     ou no terminal: baton use <job|goal.toml> claude

`claude` e `claude_api` são nomes aceitos. Essa opção usa a chave da API e é cobrada pela
Anthropic Console; não usa a assinatura do Claude Code. O limite de rodada padrão é USD 5, o de
turno é USD 50 e o limite por goal é USD 150. Os limites podem ser ajustados em `defaults.toml`.

Aliases disponíveis: codex, antigravity, claude (claude_api).
"""
