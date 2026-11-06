# OpenCode 2.2.5: investigação de sincronização

Estado: **investigação parcial**. Não implementar a integração com base apenas neste documento: falta reproduzir execução ativa, duplo Esc, reconexão e promoção com a TUI real e definir um mecanismo seguro de confirmação de entrega.

## Evidência do código v2.2.5

- A TUI exige dois acionamentos de interrupt em cinco segundos e chama `session.interrupt` com `resume: true`: [prompt/index.tsx](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/tui/src/component/prompt/index.tsx).
- [`execution.ts`](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/session/execution.ts) solicita interrupção com motivo `user`. A confirmação ocorre antes do término da limpeza. Com `resume=true`, pode acordar trabalho `steer` ou controle seguinte, enquanto prompts `queue` permanecem estacionados. A emissão de `Execution.Interrupted` acontece no encerramento da execução; `shutdown` tem semântica distinta. A API retorna se havia execução ativa, não uma garantia de limpeza concluída.
- [`inbox.ts`](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/session/inbox.ts) publica `InboxDelivered` durante promoção. A projeção de entrega remove a linha pendente e projeta a mensagem na mesma transação. A promoção de `steer` e de `queue` tem escopos diferentes e respeita controles na fila. `InboxCancelled` remove a linha e libera referências: cancelar não é entregar.
- A [API](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/protocol/src/groups/session.ts) expõe listagem, cancelamento e mudança de modalidade da inbox, além de log durável experimental e interrupt. Não foi identificada operação pública de entrega por ID. `PATCH` para `steer` acorda a execução: não é simples confirmação e pode provocar execução adicional. Não editar diretamente o SQLite, nem usar cancelamento como se preservasse histórico.
- Uma desconexão do stream HTTP, isoladamente, não demonstra duplo Esc: a solução precisa de sinal explícito associado à sessão e precisa tratar reconexões sem interromper trabalho. Ainda não está demonstrado que o log durável forneça o sinal cedo o bastante para parar o subprocesso do baton.

## Experimento isolado

Servidor `opencode v2.2.5` em localhost, porta separada, com diretórios XDG e workspace descartáveis. Criada sessão descartável; `GET /api/session/{id}/inbox` retornou lista vazia e `POST /api/session/{id}/interrupt?resume=true` retornou `interrupted: false` quando ociosa. Um prompt `queue` com `resume: false` apareceu na inbox; `DELETE` retornou 204 e esvaziou a fila, mas consultar a mensagem pelo mesmo ID retornou 404. Isto confirma que cancelar não preserva o histórico como mensagem entregue. Nenhuma sessão nem banco real foram alterados.

## Pontos abertos antes da implementação

1. Reproduzir, com execução ativa e TUI descartáveis, duplo Esc, sequência de eventos/status e chegada de steer e queue. Investigar o intervalo entre aceite da interrupção e evento terminal.
2. Reproduzir desconexão/reconexão e correlacionar eventos por sessão; demonstrar que não dispara parada.
3. Descobrir se existe mecanismo suportado de promoção/entrega externa que preserve a transação, histórico e evento de UI, sem criar execução duplicada. Caso não exista, avaliar mudança no OpenCode ou um protocolo alternativo, sem supor que cancel ou SQL direto sejam equivalentes.
4. Determinar descoberta do servidor e autenticação da instância concreta que originou a requisição; validar múltiplas instâncias, permissões e indisponibilidade sem expor segredos. O servidor isolado forneceu credencial temporária própria; isto não estabelece como descobrir a instância de produção.
5. Verificar idempotência, recuperação após falha e ordem das mensagens antes de definir um protocolo de persistência e confirmação.
