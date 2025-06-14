"""
====================================================================
REST → MCP DASHBOARD  •  v2025-06
====================================================================

4 раздела (вкладки Streamlit):

1. 💬 CHAT
   ─────────
   • Показывает историю диалога с GPT-3.5/4.
   • GPT автоматически видит список function-tools активного MCP-сервера
     и при необходимости вызывает их через /tools/call.

2. 🔄 CONVERT
   ───────────
   • Подсказка о том, что загрузка спецификации теперь в «API Setup»,
     а запуск MCP перенесён в «Chat».

3. 🗂 PROJECTS
   ───────────
   • Создание/удаление проектов.  В каждом проекте — один OpenAI-ключ.
   • Подключение нужных API-профилей из глобального каталога и их статус
     («✅ запущен» или «⏹ остановлен»).

4. ⚙️ API SETUP
   ─────────────
   • Управление глобальным каталогом API-профилей (URL, порт, auth-
     заголовки или query-ключ, имя). Профиль хранит также spec,
     enabled-map, тред сервера и логи.

---------------------------------------------------------------------
Зависимости  (requirements.txt):

    streamlit>=1.34
    requests>=2.31
    httpx>=0.27
    pyyaml>=6.0
    pydantic>=2.6
    openai>=1.25
    mcp>=0.3               # FastMCP SDK
    python-dotenv>=1.0

---------------------------------------------------------------------
Файл .env  (лежит рядом с этим app.py):

    OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

---------------------------------------------------------------------
Запуск:

    streamlit run app.py
---------------------------------------------------------------------
"""

import os, json, yaml, threading, time, copy
from typing import Dict, Tuple, Set

import streamlit as st
import requests, httpx, openai
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP


# ══════════════════════════════════════════════════════════════════
#  ПОЛЕЗНЫЕ ФУНКЦИИ                                                  #
# ══════════════════════════════════════════════════════════════════
def rerun():
    """Streamlit ≥1.30 → st.rerun, старые → experimental_rerun."""
    (getattr(st, "rerun", None) or st.experimental_rerun)()


def load_openapi(url: str) -> Dict:
    """Скачать OpenAPI/Swagger по URL и вернуть словарь."""
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    txt = r.text.lstrip()
    try:
        return json.loads(txt)  # JSON
    except json.JSONDecodeError:
        return yaml.safe_load(txt)  # YAML


def gpt_describe(spec: Dict, api_key: str):
    """Добавить/обновить description для каждого operation."""
    if not api_key:
        return
    openai.api_key = api_key
    for path, meths in spec["paths"].items():
        for method, op in meths.items():
            prompt = (
                f"Опиши назначение эндпоинта одним предложением:\n"
                f"{method.upper()} {path}"
            )
            try:
                resp = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=40,
                    temperature=0,
                )
                op["description"] = resp.choices[0].message.content.strip()
            except Exception as e:
                print("GPT error:", e)


def filter_spec(spec: Dict, allowed: Set[Tuple[str, str]]) -> Dict:
    """Вернуть копию spec, содержащую только allowed (path, method)."""
    s2 = copy.deepcopy(spec)
    for p in list(s2["paths"].keys()):
        for m in list(s2["paths"][p].keys()):
            if (p, m.lower()) not in allowed:
                s2["paths"][p].pop(m)
        if not s2["paths"][p]:
            s2["paths"].pop(p)
    return s2


def make_http_client(base: str, headers: Dict, qparams: Dict, logger):
    """httpx.AsyncClient с хуком логирования."""

    def hook(resp: httpx.Response):
        logger(f"{resp.request.method} {resp.request.url} → {resp.status_code}")

    return httpx.AsyncClient(
        base_url=base,
        headers=headers,
        params=qparams,
        timeout=20,
        event_hooks={"response": [hook]},
    )


def log_line(api: dict, msg: str):
    api.setdefault("logs", []).append(f"{time.strftime('%H:%M:%S')}  {msg}")


# ──────────────────────────────────────────────────────────────────
#  Persist projects and API catalog to disk
# ──────────────────────────────────────────────────────────────────
DATA_FILE = "dashboard.json"


def load_state():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state["projects"] = data.get("projects", {})
        cats = data.get("api_catalog", {})
        state["api_catalog"] = {
            k: {**v, "thread": None, "logs": []} for k, v in cats.items()
        }
    else:
        state["projects"] = {}
        state["api_catalog"] = {}


def save_state():
    cats = {}
    for k, v in state.get("api_catalog", {}).items():
        v2 = {**v}
        v2.pop("thread", None)
        v2.pop("logs", None)
        cats[k] = v2
    data = {"projects": state.get("projects", {}), "api_catalog": cats}
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# Каталог заранее известных API-профилей
PREDEFINED_APIS = {
    "Petstore": {
        "url": "https://petstore.swagger.io/v2/swagger.json",
        "port": 8000,
    },
}


def start_mcp(api: dict):
    """Запустить FastMCP для указанного профиля."""
    if not api.get("spec"):
        raise RuntimeError("Spec not loaded")

    allowed = {
        (p, m.lower())
        for p, m in [k.split(" ", 1)[::-1] for k, v in api["enabled"].items() if v]
    }
    spec_filtered = filter_spec(api["spec"], allowed)

    base = (api["spec"].get("servers", [{"url": ""}])[0]["url"]).rstrip("/")
    headers = {api["header_name"]: api["header_val"]} if api["header_name"] else {}
    qparams = {api["query_name"]: api["query_val"]} if api["query_name"] else {}

    client = make_http_client(base, headers, qparams, lambda m: log_line(api, m))

    mcp = FastMCP.from_openapi(
        spec_filtered,
        client,
        name=api["name"],
        host="0.0.0.0",
        port=api["port"],
    )

    def run_server():
        log_line(api, f"🚀 MCP start :{api['port']}")
        mcp.run()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    api["thread"] = t
    api["logs"] = []
    save_state()


# ══════════════════════════════════════════════════════════════════
#  ИНИЦИАЛИЗАЦИЯ SESSION_STATE                                      #
# ══════════════════════════════════════════════════════════════════
load_dotenv()
OPENAI_ENV = os.getenv("OPENAI_API_KEY", "")

st.set_page_config(page_title="REST → MCP", layout="wide")

state = st.session_state
load_state()
state.setdefault("projects", {})  # name → project-dict
state.setdefault("api_catalog", {})  # глобальные API-профили
state.setdefault("proj_sel", None)  # выбранный проект
state.setdefault("api_sel", None)  # выбранный API-профиль
state.setdefault("chat", [])  # [(role, text)]

# ══════════════════════════════════════════════════════════════════
#  SIDEBAR NAVIGATION                                               #
# ══════════════════════════════════════════════════════════════════
PAGES = ["💬 Chat", "🔄 Convert", "🗂 Projects", "⚙️ API Setup"]
state.setdefault("page", PAGES[0])
with st.sidebar:
    for p in PAGES:
        if st.button(
            p,
            key=f"nav_{p}",
            type="primary" if state["page"] == p else "secondary",
            use_container_width=True,
        ):
            state["page"] = p
            rerun()
    st.divider()
    if state.get("proj_sel"):
        st.success(f"Проект: {state['proj_sel']}")
    else:
        st.info("Проект не выбран")
page = state["page"]

# ───────────────────────────────────────────────────── Projects ───
if page == "🗂 Projects":
    st.header("🗂 Управление проектами")

    proj_names = list(state["projects"])
    chosen = st.selectbox(
        "Проект",
        ["< создать >"] + proj_names,
        index=(
            proj_names.index(state["proj_sel"]) + 1
            if state["proj_sel"] in proj_names
            else 0
        ),
    )
    creating_new = chosen == "< создать >"

    project = (
        {"name": "", "openai": OPENAI_ENV, "apis": []}
        if creating_new
        else state["projects"][chosen]
    )
    with st.form("proj_form"):
        project["name"] = st.text_input("Название проекта", project["name"])
        project["openai"] = st.text_input(
            "OpenAI API-ключ",
            project["openai"],
            type="password",
            help="Пусто → берётся из .env",
        )
        if st.form_submit_button(
            "💾 Сохранить проект", type="primary", use_container_width=True
        ):
            if not project["name"]:
                st.warning("Имя проекта обязательно.")
            else:
                state["projects"][project["name"]] = project
                state["proj_sel"] = project["name"]
                save_state()
                rerun()

    if not creating_new:
        st.divider()
        st.subheader("Подключить API-профили")
        sel = st.multiselect(
            "Доступные профили",
            list(state["api_catalog"]),
            default=project.get("apis", []),
            key="proj_api_ms",
        )
        if st.button(
            "💾 Сохранить список", key="proj_api_save", use_container_width=True
        ):
            project["apis"] = sel
            state["projects"][project["name"]] = project
            save_state()
            rerun()

        st.subheader("Текущие профили")
        for api_name in project.get("apis", []):
            cfg = state["api_catalog"].get(api_name)
            if not cfg:
                continue
            running = cfg.get("thread") and cfg["thread"].is_alive()
            badge = "✅" if running else "⏹"
            st.write(f"{badge} **{api_name}**  —  {cfg['url']}  (:{cfg['port']})")

# ───────────────────────────────────────────────────── API Setup ──
elif page == "⚙️ API Setup":
    st.header("⚙️ Настройка API-профиля")

    st.subheader("Добавить из каталога")
    predef = st.selectbox("Профиль", ["< выбрать >"] + list(PREDEFINED_APIS))
    if predef != "< выбрать >" and st.button("➕ Добавить", key="add_predef"):
        cfg = PREDEFINED_APIS[predef]
        state["api_catalog"][predef] = {
            "name": predef,
            "url": cfg["url"],
            "port": cfg.get("port", 8000),
            "header_name": "",
            "header_val": "",
            "query_name": "",
            "query_val": "",
            "spec": None,
            "enabled": {},
            "thread": None,
            "logs": [],
        }
        state["api_sel"] = predef
        rerun()

    api_names = list(state["api_catalog"])
    chosen_api = st.selectbox(
        "API-профиль",
        ["< создать >"] + api_names,
        index=(
            api_names.index(state["api_sel"]) + 1
            if state["api_sel"] in api_names
            else 0
        ),
    )
    creating_api = chosen_api == "< создать >"

    api = (
        {
            "name": "",
            "url": "",
            "port": 8000,
            "header_name": "",
            "header_val": "",
            "query_name": "",
            "query_val": "",
            "spec": None,
            "enabled": {},
            "thread": None,
            "logs": [],
        }
        if creating_api
        else state["api_catalog"][chosen_api]
    )

    with st.form("api_form"):
        col1, col2 = st.columns(2)
        with col1:
            api["name"] = st.text_input("API-имя", api["name"])
            api["url"] = st.text_input("URL спецификации", api["url"])
            api["port"] = st.number_input("Порт MCP", 1024, 65535, api["port"])
        with col2:
            api["header_name"] = st.text_input("Auth header", api["header_name"])
            api["header_val"] = st.text_input("Header value", api["header_val"])
            api["query_name"] = st.text_input("Auth query", api["query_name"])
            api["query_val"] = st.text_input("Query value", api["query_val"])

        if st.form_submit_button(
            "💾 Сохранить API", type="primary", use_container_width=True
        ):
            if not api["name"] or not api["url"]:
                st.warning("Заполните имя и URL спецификации.")
            else:
                state["api_catalog"][api["name"]] = api
                state["api_sel"] = api["name"]
                save_state()
                rerun()

    if not creating_api:
        st.divider()
        if st.button(
            "🔄 Скачать спецификацию", type="primary", use_container_width=True
        ):
            try:
                spec = load_openapi(api["url"])
            except Exception as e:
                st.error(f"Ошибка скачивания или парсинга: {e}")
                st.stop()

            gpt_describe(spec, OPENAI_ENV)
            api["spec"] = spec
            eps = {(p, m.lower()) for p, v in spec["paths"].items() for m in v}
            if not api["enabled"]:
                api["enabled"] = {f"{m} {p}": True for (p, m) in eps}

            save_state()
            rerun()

        if api.get("spec"):
            st.subheader("Включить/отключить эндпоинты")
            with st.form("ep_form"):
                cols = st.columns(2)
                for i, (p, meths) in enumerate(api["spec"]["paths"].items()):
                    for m in meths:
                        key = f"{m} {p}"
                        with cols[i % 2]:
                            api["enabled"][key] = st.checkbox(
                                key, value=api["enabled"][key]
                            )
                if st.form_submit_button("💾 Сохранить", use_container_width=True):
                    save_state()
                    rerun()

# ───────────────────────────────────────────────────── CONVERT ───
elif page == "🔄 Convert":
    st.header("🔄 Convert")
    st.info(
        "Загрузка спецификации теперь во вкладке «API Setup», запуск MCP — во вкладке «Chat»."
    )

# ───────────────────────────────────────────────────── CHAT ──────
elif page == "💬 Chat":
    st.header("💬 Тестовый чат с MCP")

    all_servers = [
        (pj_name, api_name, state["api_catalog"][api_name])
        for pj_name, pj in state["projects"].items()
        for api_name in pj.get("apis", [])
        if api_name in state["api_catalog"]
    ]
    if not all_servers:
        st.info("Нет настроенных API-профилей.")
        st.stop()

    options = [
        f"{pj}/{api}  (:{cfg['port']})"
        + (" ✅" if cfg.get("thread") and cfg["thread"].is_alive() else " ⏹")
        for pj, api, cfg in all_servers
    ]
    sel_label = st.selectbox("Выберите MCP-профиль", options)
    pj_name, api_name = sel_label.split("  ")[0].split("/")
    chat_cfg = state["api_catalog"][api_name]
    running = chat_cfg.get("thread") and chat_cfg["thread"].is_alive()

    if st.button("🚀 Запустить / Перезапустить MCP", use_container_width=True):
        try:
            start_mcp(chat_cfg)
        except Exception as e:
            st.error(f"FastMCP error: {e}")
        rerun()

    if not running:
        st.info("Сервер не запущен.")
        st.stop()

    port = chat_cfg["port"]

    # вывод истории
    for role, msg in state["chat"]:
        st.chat_message(role).markdown(msg)

    user_msg = st.chat_input("Сообщение…")
    if user_msg:
        state["chat"].append(("user", user_msg))
        st.chat_message("user").markdown(user_msg)

        # получить tools /tools/list
        try:
            tools_json = requests.post(
                f"http://localhost:{port}/tools/list", timeout=10
            ).json()
            tools = tools_json["tools"]
        except Exception as e:
            st.error(f"/tools/list error: {e}")
            st.stop()

        functions = [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            }
            for t in tools
        ]

        openai.api_key = state["projects"][pj_name]["openai"] or OPENAI_ENV
        conv = [
            {"role": "system", "content": "При необходимости используй MCP-tools."}
        ] + [{"role": r, "content": m} for r, m in state["chat"]]

        while True:
            resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo-1106", messages=conv, functions=functions
            )
            msg = resp.choices[0].message

            # если GPT хочет вызвать функцию
            if "function_call" in msg:
                fc = msg.function_call
                args = json.loads(fc.arguments or "{}")
                try:
                    result = requests.post(
                        f"http://localhost:{port}/tools/call",
                        json={"name": fc.name, "arguments": args},
                        timeout=30,
                    ).json()
                    tool_answer = result["content"][0]["text"]
                except Exception as e:
                    tool_answer = f"⚠️ MCP error: {e}"

                conv.append(
                    {
                        "role": "assistant",
                        "name": fc.name,
                        "content": None,
                        "function_call": fc,
                    }
                )
                conv.append(
                    {"role": "function", "name": fc.name, "content": tool_answer}
                )
            else:
                answer = msg.content
                state["chat"].append(("assistant", answer))
                st.chat_message("assistant").markdown(answer)
                break
