import os, json, subprocess, shlex, time
import redis
import psycopg
import httpx

r = redis.Redis(host=os.getenv("REDIS_HOST","redis"), port=int(os.getenv("REDIS_PORT","6379")), decode_responses=True)
ALERTS_STREAM = os.getenv("ALERTS_STREAM","alerts_stream")
DECISIONS_STREAM = os.getenv("DECISIONS_STREAM","decisions_stream")
ACTIONS_STREAM = os.getenv("ACTIONS_STREAM","actions_stream")
GROUP = os.getenv("WORKER_GROUP","assist_group")

DB_DSN = os.getenv("DB_DSN","postgresql://assist:strong_password_here@postgres:5432/assistdb")

USE_OLLAMA = os.getenv("USE_OLLAMA","true") == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL","ollamahttp://ollama:11434")
OPENAI_URL = os.getenv("OPENAI_BASE_URL","")
OPENAI_KEY = os.getenv("OPENAI_API_KEY","")

POLICY_SAFE_ACTIONS = {
  "restart_service": {"cmd":"sudo /usr/local/bin/assist-safe-run systemctl restart {service}", "approval":"maybe"},
  "start_service":   {"cmd":"sudo /usr/local/bin/assist-safe-run systemctl start {service}",   "approval":"maybe"},
  "stop_service":    {"cmd":"sudo /usr/local/bin/assist-safe-run systemctl stop {service}",    "approval":"admin"},
  "apt_update":      {"cmd":"sudo /usr/local/bin/assist-safe-run apt update",                  "approval":"operator"},
  "apt_upgrade":     {"cmd":"sudo /usr/local/bin/assist-safe-run apt upgrade -y",              "approval":"admin"},
  "ufw_allow":       {"cmd":"sudo /usr/local/bin/assist-safe-run ufw allow {port}/tcp",        "approval":"admin"},
}

def log_audit(conn, event_type, source, input_d=None, decision_d=None, command=None, stdout=None, stderr=None, exit_code=None, user_context=None):
    with conn.cursor() as cur:
        cur.execute("""
          INSERT INTO audit_log(event_type,source,input,decision,command,stdout,stderr,exit_code,user_context)
          VALUES(%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)
        """, (
            event_type, source,
            json.dumps(input_d) if input_d else None,
            json.dumps(decision_d) if decision_d else None,
            command, stdout, stderr, exit_code, user_context
        ))
    conn.commit()

def llm_plan(prompt):
    if USE_OLLAMA:
        # formato simples para ollama
        data = {"model":"qwen2.5:1.5b", "prompt": prompt, "stream": False}
        resp = httpx.post(f"{OLLAMA_URL}/api/generate", json=data, timeout=60)
        resp.raise_for_status()
        return resp.json().get("response","")
    else:
        headers={"Authorization": f"Bearer {OPENAI_KEY}"}
        data={"model":"gpt-4o-mini","messages":[{"role":"system","content":"You are a remediation planner. Output JSON."},{"role":"user","content":prompt}]}
        resp = httpx.post(f"{OPENAI_URL}/v1/chat/completions", headers=headers, json=data, timeout=60)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

def decide_remediation(alert):
    # Política híbrida: regras rápidas + sugestão LLM
    labels = json.loads(alert.get("labels","{}"))
    annotations = json.loads(alert.get("annotations","{}"))
    hint = annotations.get("summary","") or annotations.get("description","")

    # Regras simples
    if "InstanceDown" in labels.get("alertname",""):
        plan = {"action":"restart_service","params":{"service":"apache2"}, "reason":"Instance down - tentar reiniciar apache2"}
        return plan

    # Consultar LLM para plano
    prompt = f"""
Você é um assistente de SRE. Recebeu um alerta com labels={labels} e hint="{hint}".
Proponha UMA ação canônica no formato JSON: {{"action":"<uma_das_chaves_de_POLICY>","params":{{...}},"reason":"..."}}.
Ações válidas: {list(POLICY_SAFE_ACTIONS.keys())}.
Se não tiver certeza, use "restart_service" com "service":"<nome>" baseado no hint.
Responda apenas o JSON.
"""
    raw = llm_plan(prompt).strip()
    # Tentar extrair JSON
    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
        plan = json.loads(raw)
    except:
        plan = {"action":"restart_service","params":{"service":"nginx"},"reason":"fallback"}
    return plan

def needs_approval(action_key):
    level = POLICY_SAFE_ACTIONS.get(action_key,{}).get("approval","admin")
    # simplificação: "maybe" -> não exige se serviço for em allowlist
    return level in ("admin","operator")

ALLOWED_SERVICES = {"nginx","apache2","ssh","docker"}

def validate_params(action_key, params):
    if action_key in ("restart_service","start_service","stop_service"):
        svc = params.get("service","")
        if svc not in ALLOWED_SERVICES:
            raise ValueError(f"Serviço não permitido: {svc}")
    if action_key == "ufw_allow":
        port = int(params.get("port",0))
        if port < 1024 or port > 65535:
            raise ValueError("Porta inválida")
    return True

def run_command(cmd):
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate(timeout=120)
    return proc.returncode, out, err

def process_alerts(conn):
    r.xgroup_create(name=ALERTS_STREAM, groupname=GROUP, id="0-0", mkstream=True)
    while True:
        resp = r.xreadgroup(GROUP, "worker-1", {ALERTS_STREAM: ">"}, count=1, block=5000)
        if not resp: 
            continue
        for stream, messages in resp:
            for msg_id, fields in messages:
                # decisão
                plan = decide_remediation(fields)
                r.xadd(DECISIONS_STREAM, {"plan": json.dumps(plan), "source":"alert"})
                log_audit(conn, "decision", "worker", input_d=fields, decision_d=plan)
                # execução direta se não precisar aprovação
                try:
                    validate_params(plan["action"], plan.get("params",{}))
                    if not needs_approval(plan["action"]):
                        cmd_tpl = POLICY_SAFE_ACTIONS[plan["action"]]["cmd"]
                        cmd = cmd_tpl.format(**plan["params"])
                        code, out, err = run_command(cmd)
                        log_audit(conn, "execution", "worker", decision_d=plan, command=cmd, stdout=out, stderr=err, exit_code=code)
                    else:
                        # cria aprovação
                        with conn.cursor() as cur:
                            cur.execute("INSERT INTO approvals(requested_by, action, params, status) VALUES(%s,%s,%s::jsonb,'pending')",
                                        ("system", plan["action"], json.dumps(plan.get("params",{}))))
                        conn.commit()
                except Exception as e:
                    log_audit(conn, "decision", "worker", input_d=fields, decision_d={"error":str(e)})
                r.xack(ALERTS_STREAM, GROUP, msg_id)

def process_actions(conn):
    r.xgroup_create(name=ACTIONS_STREAM, groupname=GROUP, id="0-0", mkstream=True)
    while True:
        resp = r.xreadgroup(GROUP, "worker-1", {ACTIONS_STREAM: ">"}, count=1, block=5000)
        if not resp: 
            continue
        for stream, messages in resp:
            for msg_id, fields in messages:
                action = fields["action"]
                params = json.loads(fields["params"])
                req_by = fields.get("requested_by","api")
                try:
                    validate_params(action, params)
                    if needs_approval(action):
                        with conn.cursor() as cur:
                            cur.execute("INSERT INTO approvals(requested_by, action, params, status) VALUES(%s,%s,%s::jsonb,'pending')",
                                        (req_by, action, json.dumps(params)))
                        conn.commit()
                        log_audit(conn, "decision", "api", input_d=fields, decision_d={"approval":"pending"})
                    else:
                        cmd_tpl = POLICY_SAFE_ACTIONS[action]["cmd"]
                        cmd = cmd_tpl.format(**params)
                        code, out, err = run_command(cmd)
                        log_audit(conn, "execution", "api", decision_d={"action":action,"params":params}, command=cmd, stdout=out, stderr=err, exit_code=code, user_context=req_by)
                except Exception as e:
                    log_audit(conn, "decision", "api", input_d=fields, decision_d={"error":str(e)}, user_context=req_by)
                r.xack(ACTIONS_STREAM, GROUP, msg_id)

def process_approved(conn):
    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT id, action, params FROM approvals WHERE status='approved' ORDER BY id ASC LIMIT 10")
            rows = cur.fetchall()
        for id_, action, params in rows:
            p = json.loads(params)
            try:
                validate_params(action, p)
                cmd_tpl = POLICY_SAFE_ACTIONS[action]["cmd"]
                cmd = cmd_tpl.format(**p)
                code, out, err = run_command(cmd)
                log_audit(conn, "execution", "approval", decision_d={"action":action,"params":p}, command=cmd, stdout=out, stderr=err, exit_code=code)
            finally:
                with conn.cursor() as cur:
                    cur.execute("UPDATE approvals SET status='reconciled' WHERE id=%s", (id_,))
                conn.commit()
        time.sleep(5)

if __name__ == "__main__":
    with psycopg.connect(DB_DSN) as conn:
        pid = os.fork()
        if pid == 0:
            process_alerts(conn)
        else:
            process_actions(conn)
