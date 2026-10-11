import os
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import pandas as pd
import streamlit as st

# -----------------------------
# TRUTH SCAN — Streamlit MVP
# -----------------------------
APP_TITLE = "TRUTH SCAN"
DB_PATH = "truth_scan.db"
TAVILY_URL = "https://api.tavily.com/search"

st.set_page_config(
    page_title="TRUTH SCAN | AI Fact Check",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---- Visual design ----
st.markdown(
    """
    <style>
    :root { color-scheme: dark; }
    .stApp { background: #0b1020; }
    [data-testid="stSidebar"] { background: #10182b; border-right: 1px solid #26334d; }
    .block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1450px; }
    .hero {
      padding: 1.45rem 1.6rem; border: 1px solid #263858; border-radius: 18px;
      background: linear-gradient(120deg, #111c33 0%, #142a46 58%, #17213b 100%);
      margin-bottom: 1.2rem;
    }
    .eyebrow { color: #70b7ff; letter-spacing: .16em; font-size: .78rem; font-weight: 800; }
    .hero h1 { margin: .2rem 0 .35rem 0; font-size: 2.5rem; color: #f4f7ff; }
    .hero p { color: #b7c4dc; margin: 0; }
    .panel {
      border: 1px solid #27354e; background: #111a2d; padding: 1rem 1.15rem;
      border-radius: 14px; margin-bottom: .75rem;
    }
    .muted { color: #9aabc6; font-size: .9rem; }
    .tag { display:inline-block; padding:.25rem .55rem; border-radius: 999px;
      border:1px solid #334766; color:#c8d8f2; font-size:.78rem; margin-right:.3rem; }
    div[data-testid="stMetric"] {
      background: #111a2d; border: 1px solid #27354e; padding: 1rem;
      border-radius: 14px;
    }
    .stButton > button { border-radius: 10px; font-weight: 700; min-height: 2.7rem; }
    a { color: #80c0ff !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---- Database ----
def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                claim TEXT NOT NULL,
                verdict TEXT NOT NULL,
                confidence TEXT NOT NULL,
                summary TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

def make_user_key(name: str) -> str:
    # This is a simple namespace, NOT authentication.
    return hashlib.sha256(name.strip().casefold().encode("utf-8")).hexdigest()

def save_check(user_key, display_name, claim, verdict, confidence, summary, evidence):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO checks
            (user_key, display_name, claim, verdict, confidence, summary, evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_key, display_name, claim, verdict, confidence, summary,
                json.dumps(evidence, ensure_ascii=False), now
            ),
        )
        conn.commit()

def load_checks(user_key):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM checks WHERE user_key = ? ORDER BY id DESC", (user_key,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["evidence"] = json.loads(item.pop("evidence_json"))
        except Exception:
            item["evidence"] = []
        result.append(item)
    return result

def delete_user_history(user_key):
    with db_connect() as conn:
        conn.execute("DELETE FROM checks WHERE user_key = ?", (user_key,))
        conn.commit()

init_db()

# ---- Search API ----
def get_secret(name, default=""):
    try:
        value = st.secrets.get(name, default)
        return str(value).strip() if value is not None else default
    except Exception:
        return os.getenv(name, default).strip()

def search_web(query, max_results=5):
    api_key = get_secret("TAVILY_API_KEY")
    if not api_key:
        return [], "Tavily API 키가 설정되지 않았습니다."
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "topic": "general",
        "max_results": int(max_results),
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        response = requests.post(TAVILY_URL, json=payload, timeout=25)
        if response.status_code == 401 or response.status_code == 403:
            return [], "API 키 인증에 실패했습니다. Secrets 설정을 확인해 주세요."
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("results", []):
            url = item.get("url", "")
            # Only accept normal web URLs.
            if not url.startswith(("https://", "http://")):
                continue
            results.append({
                "title": item.get("title") or "제목 없음",
                "url": url,
                "content": item.get("content") or "",
                "score": item.get("score"),
                "domain": urlparse(url).netloc,
            })
        return results, None
    except requests.Timeout:
        return [], "검색 요청 시간이 초과됐습니다. 잠시 후 다시 시도해 주세요."
    except requests.RequestException as exc:
        return [], f"검색 서비스에 연결하지 못했습니다: {exc.__class__.__name__}"
    except (ValueError, TypeError):
        return [], "검색 서비스에서 읽을 수 없는 응답을 받았습니다."

# ---- Conservative evidence assessment ----
def assess_evidence(claim, evidence):
    if not evidence:
        return {
            "verdict": "판정 보류",
            "confidence": "판정 불가",
            "summary": "관련 검색 근거를 확보하지 못했습니다. 따라서 참·거짓을 판정하지 않습니다.",
        }

    usable = [
        e for e in evidence
        if len((e.get("content") or "").strip()) >= 60
        and e.get("url", "").startswith(("http://", "https://"))
    ]
    distinct_domains = {e.get("domain") for e in usable if e.get("domain")}
    if len(usable) < 2 or len(distinct_domains) < 2:
        return {
            "verdict": "판정 보류",
            "confidence": "근거 부족",
            "summary": (
                "검색 결과가 없거나 독립적인 출처가 충분하지 않습니다. "
                "현재 자료만으로는 이 주장을 참·거짓으로 단정할 수 없습니다."
            ),
        }

    # No LLM-based truth judgement is made here. Search snippets are not proof.
    return {
        "verdict": "추가 검토 필요",
        "confidence": "제한적 근거",
        "summary": (
            "관련 검색 결과를 찾았지만, 검색 요약문만으로 주장의 참·거짓을 확정하지 않습니다. "
            "아래 출처의 원문과 발행 기관, 날짜, 연구 방법을 직접 확인해 주세요."
        ),
    }

# ---- Session state ----
if "last_result" not in st.session_state:
    st.session_state.last_result = None

# ---- Sidebar ----
with st.sidebar:
    st.markdown("## 🔎 TRUTH SCAN")
    st.caption("AI-assisted claim review")
    st.markdown("---")
    page = st.radio(
        "메뉴",
        ["주장 검증", "내 검증 기록", "통계 대시보드", "사용 안내"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    display_name = st.text_input(
        "사용자 이름",
        value=st.session_state.get("display_name", ""),
        placeholder="예: study_user_01",
        help="기록을 구분하기 위한 이름입니다. 비밀번호가 아니며 본인 인증 기능은 없습니다.",
    ).strip()
    st.session_state.display_name = display_name
    if display_name:
        st.caption("현재 기록 공간: " + display_name)
    else:
        st.caption("기록 저장 전 사용자 이름을 입력해 주세요.")
    st.markdown("---")
    st.markdown(
        '<span class="tag">DARK ANALYTICS</span><span class="tag">EVIDENCE FIRST</span>',
        unsafe_allow_html=True,
    )
    st.caption("검색 결과는 사실의 증명이 아닙니다.")

# ---- Hero ----
st.markdown(
    """
    <div class="hero">
      <div class="eyebrow">EVIDENCE • CONTEXT • TRANSPARENCY</div>
      <h1>TRUTH SCAN</h1>
      <p>주장을 검토하고, 근거를 확인하고, 모르는 것은 모른다고 표시합니다.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if page == "주장 검증":
    left, right = st.columns([1.25, 0.75], gap="large")
    with left:
        st.subheader("검증할 주장")
        claim = st.text_area(
            "주장 또는 뉴스 내용을 입력하세요",
            placeholder="예: 어떤 연구에서 특정 습관이 기억력을 두 배 향상시킨다고 밝혔다.",
            height=150,
            max_chars=1800,
            label_visibility="collapsed",
        )
        search_query = st.text_input(
            "검색어 (선택)",
            placeholder="비워 두면 입력한 주장을 그대로 검색합니다.",
            help="정확한 검색을 위해 핵심 문장, 기관명, 연구 제목 등을 넣을 수 있습니다.",
        )
        result_count = st.slider("검색 결과 수", min_value=3, max_value=8, value=5)
        run_check = st.button("🔎 근거 검색 및 검토", type="primary", use_container_width=True)
        if run_check:
            if not claim.strip():
                st.warning("먼저 검증할 주장을 입력해 주세요.")
            elif len(claim.strip()) < 8:
                st.warning("조금 더 구체적인 주장을 입력해 주세요.")
            else:
                query = search_query.strip() or claim.strip()
                with st.spinner("웹에서 관련 근거를 검색하고 있습니다..."):
                    evidence, error = search_web(query, result_count)
                if error:
                    st.error(error)
                    st.info("API 키 설정 전에도 화면과 기록 기능은 사용할 수 있습니다.")
                else:
                    assessment = assess_evidence(claim.strip(), evidence)
                    st.session_state.last_result = {
                        "claim": claim.strip(),
                        "query": query,
                        "evidence": evidence,
                        **assessment,
                    }
                    st.rerun()

    with right:
        st.markdown("### 검토 원칙")
        st.markdown(
            """
            <div class="panel">
              <div class="tag">01 · 검색</div>
              <p>관련 자료와 출처 링크를 모읍니다.</p>
              <div class="tag">02 · 교차 확인</div>
              <p>서로 다른 출처가 있는지 살펴봅니다.</p>
              <div class="tag">03 · 신중한 판정</div>
              <p>근거가 부족하면 판정 보류로 남깁니다.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.warning(
            "현재 버전은 검색 결과를 모으고 근거 수준을 보수적으로 분류합니다. "
            "AI가 원문 전체를 읽고 사실 여부를 확정하는 기능은 포함하지 않습니다."
        )

    result = st.session_state.last_result
    if result:
        st.markdown("---")
        st.subheader("검토 결과")
        m1, m2, m3 = st.columns(3)
        m1.metric("현재 판정", result["verdict"])
        m2.metric("근거 수준", result["confidence"])
        m3.metric("검색 출처 수", len(result["evidence"]))
        st.markdown("**입력한 주장**")
        st.info(result["claim"])
        st.markdown("**요약**")
        st.write(result["summary"])
        if result.get("supporting_points"):
            st.markdown("**근거 요약**")
            for point in result["supporting_points"]:
                st.markdown(f"- {point}")
        if result.get("limitations"):
            st.markdown("**한계 및 주의점**")
            for limitation in result["limitations"]:
                st.markdown(f"- {limitation}")
        if result.get("source_ids"):
            st.caption("AI가 참고한 검색 결과 번호: " + ", ".join(map(str, result["source_ids"])))

        st.markdown("### 검색된 출처")
        if not result["evidence"]:
            st.info("표시할 검색 출처가 없습니다.")
        for idx, item in enumerate(result["evidence"], start=1):
            with st.container(border=True):
                st.markdown(f"**{idx}. [{item['title']}]({item['url']})**")
                st.caption(item.get("domain", ""))
                st.write(item.get("content") or "검색 요약이 제공되지 않았습니다.")
                if item.get("score") is not None:
                    st.caption(f"검색 관련도 점수: {item['score']:.2f} · 사실일 확률을 뜻하지 않습니다.")

        if display_name:
            if st.button("💾 이 검토를 내 기록에 저장", use_container_width=False):
                save_check(
                    make_user_key(display_name),
                    display_name,
                    result["claim"],
                    result["verdict"],
                    result["confidence"],
                    result["summary"],
                    result["evidence"],
                )
                st.success("검토 기록을 저장했습니다.")
        else:
            st.caption("기록을 저장하려면 왼쪽 메뉴에서 사용자 이름을 입력하세요.")

elif page == "내 검증 기록":
    st.subheader("내 검증 기록")
    if not display_name:
        st.info("왼쪽 메뉴에서 사용자 이름을 입력하면 해당 이름으로 저장된 기록을 볼 수 있습니다.")
    else:
        records = load_checks(make_user_key(display_name))
        if not records:
            st.info("아직 저장된 기록이 없습니다. '주장 검증'에서 결과를 저장해 보세요.")
        else:
            st.caption(f"총 {len(records)}건 · 사용자 이름 기준으로 분리된 기록")
            for record in records:
                with st.expander(f"{record['verdict']} · {record['claim'][:90]}"):
                    st.caption(f"저장 시각(UTC): {record['created_at']}")
                    st.write(record["summary"])
                    st.write(f"근거 수준: **{record['confidence']}**")
                    for item in record["evidence"]:
                        st.markdown(f"- [{item.get('title','출처')}]({item.get('url','#')})")
            st.download_button(
                "기록 CSV 다운로드",
                data=pd.DataFrame([
                    {
                        "created_at_utc": r["created_at"],
                        "claim": r["claim"],
                        "verdict": r["verdict"],
                        "confidence": r["confidence"],
                        "summary": r["summary"],
                    }
                    for r in records
                ]).to_csv(index=False).encode("utf-8-sig"),
                file_name="truth_scan_history.csv",
                mime="text/csv",
            )
            st.warning("기록 삭제는 되돌릴 수 없습니다. 사용자 이름만으로는 다른 사람이 기록을 열람하는 것을 막을 수 없습니다.")
            confirm = st.checkbox("이 이름으로 저장된 모든 기록을 삭제하겠습니다.")
            if st.button("🗑️ 내 기록 전체 삭제", disabled=not confirm):
                delete_user_history(make_user_key(display_name))
                st.success("해당 이름의 기록을 삭제했습니다.")
                st.rerun()

elif page == "통계 대시보드":
    st.subheader("검증 통계 대시보드")
    if not display_name:
        st.info("왼쪽 메뉴에서 사용자 이름을 입력하면 해당 기록의 통계를 볼 수 있습니다.")
    else:
        records = load_checks(make_user_key(display_name))
        if not records:
            st.info("통계를 표시할 기록이 없습니다. 검토 결과를 저장해 보세요.")
        else:
            df = pd.DataFrame(records)
            total = len(df)
            unique_claims = df["claim"].nunique()
            needs_review = int(df["verdict"].isin(["판정 보류", "추가 검토 필요"]).sum())
            c1, c2, c3 = st.columns(3)
            c1.metric("총 검증 기록", total)
            c2.metric("서로 다른 주장", unique_claims)
            c3.metric("추가 검토 필요", needs_review)
            st.markdown("### 판정 분포")
            verdict_counts = df["verdict"].value_counts()
            st.bar_chart(verdict_counts)
            st.markdown("### 최근 활동")
            df["date"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True).dt.date
            daily = df.groupby("date").size().rename("검증 건수")
            st.line_chart(daily)
            st.markdown("### 최근 기록")
            st.dataframe(
                df[["created_at", "claim", "verdict", "confidence"]].rename(
                    columns={
                        "created_at": "저장 시각(UTC)",
                        "claim": "주장",
                        "verdict": "판정",
                        "confidence": "근거 수준",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

elif page == "사용 안내":
    st.subheader("사용 안내")
    st.markdown(
        """
        **1. API 키 설정**
        - Tavily 키는 웹 검색에 사용하고, Gemini 키는 검색 결과를 바탕으로 한 보조 분석에 사용합니다.
        - API 키는 GitHub 코드에 적지 말고 Streamlit Secrets에 입력합니다.

        **2. 사용자 이름 입력**
        - 기록을 구분할 별칭을 입력합니다.
        - 이 버전은 로그인이나 비밀번호 인증을 제공하지 않습니다. 개인정보나 비밀 정보를 입력하지 마세요.

        **3. 주장 검색**
        - 검증할 주장을 입력하고 검색을 실행합니다.
        - 검색 결과의 제목과 요약, 출처 링크를 확인합니다.

        **4. 근거를 직접 확인**
        - 검색 결과의 관련도 점수는 사실일 확률이 아닙니다.
        - 원문 발행 날짜, 저자, 기관, 연구 방법, 반대 증거를 확인하세요.

        **5. 기록과 통계**
        - 결과를 기록에 저장하면 기록 페이지와 대시보드에서 확인할 수 있습니다.
        - 기록은 현재 실행 환경의 SQLite 파일에 저장됩니다. 호스팅 환경의 파일이 영구 보존된다고 보장할 수 없으므로 중요한 기록은 CSV로 내려받으세요.

        **6. 판정 기준**
        - 출처가 없거나 부족하면 '판정 보류'로 표시합니다.
        - AI 분석은 검색 스니펫에 기반한 보조 의견이며, 실제 사실 확인을 대신하지 않습니다.
        - API 오류나 근거 부족 시 억지로 참·거짓을 판정하지 않습니다.
        """
    )
    st.markdown("### API 상태")
    if get_secret("TAVILY_API_KEY"):
        st.success("Tavily 검색 API 키가 설정되어 있습니다.")
    else:
        st.warning("TAVILY_API_KEY가 설정되지 않았습니다. 검색 기능을 쓰려면 Secrets를 설정하세요.")
    if get_secret("GEMINI_API_KEY"):
        st.success("Gemini API 키가 설정되어 있습니다. 검색 결과 기반 보조 분석을 사용할 수 있습니다.")
    else:
        st.info("GEMINI_API_KEY가 없으면 자동 판정 대신 보수적인 근거 부족 안내만 표시합니다.")

st.markdown("---")
st.caption(
    "TRUTH SCAN은 학습·탐구용 도구입니다. 검색 결과는 사실의 증명이 아니며, "
    "중요한 판단은 신뢰할 수 있는 원문과 독립적인 출처를 직접 확인하세요."
)
