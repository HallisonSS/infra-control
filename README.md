# ai-assistent

1) Requisitos e Arquitetura
   
   Objetivo: o assistente deve:
   Monitorar recursos (CPU, RAM, disco, processos, serviços, portas).
   Ler logs e alertas.
   Decidir ações de remediação com base em regras e/ou LLM.
   Executar ações pré-aprovadas e registrar tudo (entrada, decisão, comando, saída).
   Solicitar aprovação humana quando necessário (RBAC).
   Expor API e painel.
   
   Componentes: observabilidade/: Prometheus, Alertmanager, Grafana, Loki, Promtail, Node Exporter
   bus/: Redis
   agent/: FastAPI (API), Celery worker (tarefas), Regras/Políticas, Conectores LLM
   executor/: Playbooks Ansible, scripts shell wrappers, whitelists sudoers
   db/: Postgres (RBAC, auditoria, estado)
   llm/: Ollama (modelos locais) ou conector externo.
   
   Fluxo:
   Exporters e Promtail enviam métricas/logs.
   Alertmanager gera alerta → publica em Redis.
   Worker lê o evento, aplica políticas e, quando indicado, consulta LLM para plano de ação.
   Executor aplica ação segura (whitelist) e grava auditoria no Postgres.
   API expõe status, aprovações pendentes e histórico.
   Grafana dashboards e anotações.

3) Preparação do servidor Ubuntu
   Ubuntu 22.04 LTS ou 24.04 LTS
   Usuário com sudo sem senha? Não. Usaremos sudo com regras específicas.
   Atualize e instale base:
      sudo apt update && sudo apt -y upgrade
      sudo apt install -y git curl wget python3-pip python3-venv build-essential ufw
  Firewall básico (ajuste portas conforme necessidade):
      sudo ufw allow OpenSSH
      sudo ufw allow 3000/tcp # Grafana
      sudo ufw allow 9090/tcp # Prometheus
      sudo ufw allow 9093/tcp # Alertmanager
      sudo ufw allow 8000/tcp # API do agente
      sudo ufw enable

Docker e Compose:
     curl -fsSL get.docker.com | sudo sh

     sudo usermod -aG docker $USER newgrp docker
     sudo curl -L "github.com" -o /usr/local/bin/docker-compose
     sudo chmod +x /usr/local/bin/docker-compose
     docker --version && docker-compose version

3) Estrutura do repositório
   Crie diretórios:

     mkdir -p ai-assistente/{observability,agent,executor,db,llm,bus,configs,deploy}
     cd ai-assistente
     git init

4) Banco de dados (RBAC e auditoria)
   db/docker-compose.snippet.yml
     schema inicial (db/init.sql)
   Suba o Postgres e aplique o schema:
     sudo docker run --rm -v $(pwd)/db:/mnt -e PGPASSWORD=strong_password_here --network host postgres:16 psql -h 127.0.0.1 -U assist -d assistdb -f /mnt/init.sql
   (ou use entrypoint init no compose final.)

5) Barramento de eventos (Redis)
   Tópicos:
      streams: alerts_stream, actions_stream, decisions_stream

6) Observabilidade
   Node Exporter (métricas do host): observability/node_exporter/docker-compose.snippet.yml
   Prometheus: observability/prometheus/prometheus.yml
   observability/prometheus/docker-compose.snippet.yml
   Alertmanager: observability/alertmanager/alertmanager.yml
   observability/alertmanager/docker-compose.snippet.yml
   Loki e Promtail (logs): observability/loki/config.yaml
   observability/promtail/config.yaml
   observability/loki/docker-compose.snippet.yml
   observability/promtail/docker-compose.snippet.yml
   Grafana: observability/grafana/docker-compose.snippet.yml
   Após subir, adicione data sources Prometheus -prometheu- e Loki -loki- e importe dashboards de Node Exporter.

7) LLM local (Ollama) ou remoto
   Ollama: llm/docker-compose.snippet.yml
   Baixe modelo:
        curl localhost -d '{"name":"llama3.1:8b"}'
   Alternativa: provedor externo compatível com OpenAI API

   Configure variável OPENAI_API_KEY e OPENAI_BASE_URL (se necessário).

8) Executor seguro (sudo, whitelists e Ansible)
   Usuário de execução:
     sudo adduser assistbot
     sudo usermod -aG sudo assistbot
   Whitelist sudoers:
     sudo visudo -f /etc/sudoers.d/assistbot Conteúdo mínimo (ajuste caminhos):
             Defaults:assistbot !requiretty
             assistbot ALL=(root) NOPASSWD: /usr/local/bin/assist-safe-run, /usr/bin/systemctl, /usr/bin/journalctl, /usr/bin/apt, /usr/sbin/service, /usr/sbin/ufw
   Wrapper assist-safe-run: executor/assist-safe-run:
   Instalação:
        sudo install -m 0755 executor/assist-safe-run /usr/local/bin/assist-safe-run
   Playbooks Ansible (opcional, para mudanças complexas): executor/ansible/site.yml (exemplos: reiniciar serviço, rotação de logs, liberar porta UFW). Instale ansible no container worker para rodar local via connection=local.

10) Agente (API + Worker)
   requirements.txt
   agent/app/main.py (API)
   agent/worker/worker.py (decisão + execução)
   agent/Dockerfile
   Worker em container separado (ou use mesmo image com entrypoint diferente).

11) Compose unificado
    deploy/docker-compose.yml

12) Regras de alerta para Prometheus
    Crie arquivo de regras: observability/prometheus/alerts.yml
       Recarregue Prometheus:
          sudo docker-compose restart prometheus

13) RBAC e aprovações
    Endpoints adicionais (exemplo rápido):
    agent/app/admin.py (exponha via API do FastAPI):
    Registre o router no main.py:
    E adicione DB_DSN ao serviço agent-api no compose.
    Fluxo de aprovação:
          Listar pendências: GET /admin/approvals
          Aprovar/Rejeitar: POST /admin/approvals/decide
          Um pequeno cron no worker pode varrer aprovações “approved” e executar.
    No worker, adicione loop para approvals aprovadas:
    E chame em paralelo (fork/thread) no main.

14) Segurança e boas práticas
    Minimizar privilégios: permitir via sudoers apenas comandos necessários. Evitar curingas.
    Sanitização de parâmetros: já validamos serviços e portas. Expanda validações conforme novos comandos.
    Segregação de usuários: containers não devem rodar como root quando possível.
    TLS/Autenticação na API: use um reverse proxy (Caddy/Traefik/Nginx) com TLS e um JWT simples para /admin e /actions.
    Backups: volumes do Postgres e Grafana.
    Auditoria imutável: exporte audit_log para Loki ou S3 periodicamente.

15) Segurança e boas práticas
    Minimizar privilégios: permitir via sudoers apenas comandos necessários. Evitar curingas.
    Sanitização de parâmetros: já validamos serviços e portas. Expanda validações conforme novos comandos.
    Segregação de usuários: containers não devem rodar como root quando possível.
    TLS/Autenticação na API: use um reverse proxy (Caddy/Traefik/Nginx) com TLS e um JWT simples para /admin e /actions.
    Backups: volumes do Postgres e Grafana.
    Auditoria imutável: exporte audit_log para Loki ou S3 periodicamente.

16) Testes de ponta a ponta
    Saúde da API:
        curl localhost
    Disparo de ação manual:
        curl -X POST localhost -H "Content-Type: application/json" -d '{"action":"restart_service","params":{"service":"nginx"},"requested_by":"operator1"}'
        Verifique /admin/approvals (se precisar aprovação).
    Gerar alerta manual:
        Use Prometheus rules com thresholds baixos temporariamente ou envie um webhook de teste para /alerts simulando Alertmanager.
    Verificar execução:
      Consultar audit_log no Postgres:
        sudo docker exec -it psql -U assist -d assistdb -c "SELECT id,event_type,source,command,exit_code,created_at FROM audit_log ORDER BY id DESC LIMIT 20;"
    Dashboards: acesse Grafana em :3000, adicione datasources e dashboards do Node Exporter e Loki.

17) Escalonamento e Kubernetes (opcional)
    Empacote cada serviço em Helm Charts ou Kustomize (Prometheus Operator, Loki stack, Grafana, Redis, Postgres operado).
    Use KEDA para escalonar worker baseado em tamanho de stream Redis.
    Service Mesh (mTLS) e NetworkPolicies para trancar comunicação.
    External Secrets para gerenciar credenciais.

18) Operação contínua
    Política de remediação: mantenha POLICY_SAFE_ACTIONS versionada; pull requests exigem revisão de segurança.
    Rotina de updates: patch mensal dos containers e do host.
    Observabilidade do próprio agente: exponha métricas Prometheus do FastAPI e do worker (latência, ações/h, falhas).
    Post-mortem automático: o worker pode anexar comando “journalctl -u --since -2h” ao audit_log quando falhar.

19) Escalonamento e Kubernetes (opcional)
    Empacote cada serviço em Helm Charts ou Kustomize (Prometheus Operator, Loki stack, Grafana, Redis, Postgres operado).
    Use KEDA para escalonar worker baseado em tamanho de stream Redis.
    Service Mesh (mTLS) e NetworkPolicies para trancar comunicação.
    External Secrets para gerenciar credenciais.

20) Operação contínua
    Política de remediação: mantenha POLICY_SAFE_ACTIONS versionada; pull requests exigem revisão de segurança.
    Rotina de updates: patch mensal dos containers e do host.
    Observabilidade do próprio agente: exponha métricas Prometheus do FastAPI e do worker (latência, ações/h, falhas).
    Post-mortem automático: o worker pode anexar comando “journalctl -u --since -2h” ao audit_log quando falhar.

21) Exemplos de ações adicionais
    Limpeza de disco segura: playbook que remove logs rotacionados antigos em /var/log com retention configurável.
    Auto-recuperação de Docker: restart do docker ou containers específicos na allowlist.
    Abertura temporária de porta: ufw allow 8080/tcp com anotação e cron para reverter em X minutos.

22) Check-list final
    Docker Compose sobe sem erros.
    Prometheus coleta Node Exporter.
    Alertmanager entrega webhooks na API.
    Redis recebe mensagens nos streams.
    Worker toma decisões e registra auditoria.
    Executor aplica apenas comandos whitelisted via assist-safe-run.
    RBAC/approvals funcionando.
    LLM responde e sugere plano coerente.
    Grafana mostra painéis e logs.







