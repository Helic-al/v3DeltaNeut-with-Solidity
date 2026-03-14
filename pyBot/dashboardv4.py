import os
from datetime import datetime, timedelta

import boto3
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from plotly.subplots import make_subplots

# --- 設定 ---
load_dotenv("./.env")
AWS_ACCESS_KEY = os.environ.get("AWS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET")
REGION_NAME = "ap-northeast-1"

# ログファイルのパス
LOG_FILE_PATH = "bot_log.log"
ORDER_LOG_PATH = "orderlog.log"

# ページ設定 (1回だけ呼ぶ)
st.set_page_config(page_title="Bot Asset Manager", layout="wide")
st.title("🤖 Trading Bot Dashboard v4")


# --- ログ表示用関数 ---
@st.fragment(run_every=1)
def display_realtime_logs():
    st.subheader("📜 Real-time Logs")
    if os.path.exists(LOG_FILE_PATH):
        with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
            last_lines = lines[-30:]
            log_content = "".join(last_lines)
            st.text_area(
                "Console Output", value=log_content, height=400, key="log_area"
            )
    else:
        st.warning("ログファイルが見つかりません。Botは起動していますか？")


# --- DynamoDB接続 ---
@st.cache_resource
def get_dynamodb_resource():
    return boto3.resource(
        "dynamodb",
        region_name=REGION_NAME,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
    )


dynamodb = get_dynamodb_resource()
table = dynamodb.Table("DeltaNeutralize")


# --- データ取得 ---
@st.cache_data(ttl=300)
def load_data():
    try:
        response = table.scan()
        items = response.get("Items", [])

        if not items:
            return pd.DataFrame()

        df = pd.DataFrame(items)

        cols_to_convert = [
            "total_equity",
            "uni_value",
            "hl_value",
            "eth_price",
            "lp_delta",
            "net_delta",
            "raw_net_delta",
            "funding_fees",
            "step_pnl",
            "cum_pnl",
        ]

        for col in cols_to_convert:
            if col in df.columns:
                df[col] = df[col].apply(lambda x: float(x) if x is not None else 0.0)
            else:
                df[col] = 0.0

        df = df.sort_values("timestamp").reset_index(drop=True)
        return df
    except Exception as e:
        st.error(f"データ取得エラー: {e}")
        return pd.DataFrame()


# --- リバランス不連続検出 ---
def detect_rebalance_points(df, threshold_pct=0.01):
    """
    total_equityの隣接データ点間の変化率が閾値を超えた点を検出。
    Returns: リバランスと判定されたインデックスのリスト
    """
    if len(df) < 2:
        return []
    pct_change = df["total_equity"].pct_change().abs()
    rebalance_indices = df.index[pct_change > threshold_pct].tolist()
    return rebalance_indices


def get_display_df(df, rebalance_indices, manual_start_idx=None):
    """
    リバランス点またはマニュアル指定に基づいてグラフ表示範囲を決定。
    """
    start_idx = 0

    # リバランス不連続がある場合、最後の不連続点の次から表示
    if rebalance_indices:
        last_rebalance = rebalance_indices[-1]
        start_idx = last_rebalance

    # マニュアル指定がある場合はそちらを優先
    if manual_start_idx is not None:
        start_idx = manual_start_idx

    return df.iloc[start_idx:].copy()


def downsample(df, max_points=1000):
    """データ点が多すぎる場合に等間隔で間引く"""
    if len(df) > max_points:
        step = len(df) // max_points
        return df.iloc[::step].copy()
    return df.copy()


# --- パフォーマンス統計を計算 ---
def calc_performance_stats(df):
    """期間リターン、最大ドローダウン等の統計を計算"""
    stats = {}
    if df.empty or len(df) < 2:
        return stats

    equity = df["total_equity"]
    stats["start_equity"] = equity.iloc[0]
    stats["end_equity"] = equity.iloc[-1]
    stats["abs_return"] = stats["end_equity"] - stats["start_equity"]
    stats["pct_return"] = (stats["abs_return"] / stats["start_equity"]) * 100

    # 最大ドローダウン
    cummax = equity.cummax()
    drawdown = (equity - cummax) / cummax
    stats["max_drawdown_pct"] = drawdown.min() * 100

    # 期間
    stats["start_time"] = df["timestamp"].iloc[0]
    stats["end_time"] = df["timestamp"].iloc[-1]

    # 累積PnL
    stats["cum_pnl"] = df["cum_pnl"].iloc[-1]

    # 累積ファンディング
    stats["funding_fees"] = df["funding_fees"].iloc[-1]

    return stats


# ====================================
# メイン UI
# ====================================

# リロードボタン
if st.button("🔄 データ更新 (Refresh)"):
    st.cache_data.clear()
    st.rerun()

# データロード
df = load_data()

if df.empty:
    st.warning(
        "データが見つかりませんでした。Botが稼働し、DynamoDBにデータが保存されているか確認してください。"
    )
    st.stop()

# --- サイドバー設定 ---
st.sidebar.header("⚙️ 表示設定")

# 不連続閾値
threshold_pct = (
    st.sidebar.slider(
        "リバランス検出閾値 (%)",
        min_value=0.1,
        max_value=5.0,
        value=1.0,
        step=0.1,
        help="Total Equityの変化率がこの閾値を超えた場合にリバランスと判定します",
    )
    / 100
)

# リバランス点検出
rebalance_indices = detect_rebalance_points(df, threshold_pct)

# 手動表示開始位置
auto_start = st.sidebar.checkbox("自動 (最後のリバランス以降を表示)", value=True)
manual_start_idx = None
if not auto_start:
    manual_start_idx = st.sidebar.slider(
        "表示開始位置",
        min_value=0,
        max_value=len(df) - 1,
        value=0,
    )

# 表示用データ
df_filtered = get_display_df(
    df, rebalance_indices if auto_start else [], manual_start_idx
)
df_display = downsample(df_filtered)

# --- 最新ステータス ---
latest = df.iloc[-1]

col_m1, col_m2, col_m3, col_m4 = st.columns(4)
col_m1.metric("💰 総資産 (Total)", f"${latest['total_equity']:,.2f}")
col_m2.metric("📊 Net Delta", f"{latest['net_delta']:.4f} ETH")
col_m3.metric("📈 ETH Price", f"${latest['eth_price']:,.0f}")
col_m4.metric("💵 Cum PnL", f"${latest['cum_pnl']:,.2f}")

# リバランス検出情報表示
if rebalance_indices:
    st.info(
        f"ℹ️ {len(rebalance_indices)}件のリバランスイベントを検出。"
        f"表示範囲: {df_filtered['timestamp'].iloc[0]} 〜 {df_filtered['timestamp'].iloc[-1]} "
        f"({len(df_filtered)}データ点)"
    )

# ====================================
# メインコンテンツ (タブ構成)
# ====================================
col_main, col_logs = st.columns([2, 1])

with col_main:
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "📈 資産推移",
            "⚖️ デルタ分析",
            "💹 PnL分析",
            "📊 統計サマリー",
            "🔄 リバランス履歴",
        ]
    )

    # ============ TAB 1: 資産推移 ============
    with tab1:
        # --- Total Equity のみをプロット (Y軸スケールをデータ範囲に合わせる) ---
        st.subheader("Total Equity 推移")
        fig_equity = go.Figure()
        fig_equity.add_trace(
            go.Scatter(
                x=df_display["timestamp"],
                y=df_display["total_equity"],
                mode="lines",
                name="Total Equity",
                line=dict(color="#00CC96", width=2),
            )
        )
        # Y軸をデータの変動範囲に合わせて拡大表示
        eq_min = df_display["total_equity"].min()
        eq_max = df_display["total_equity"].max()
        eq_margin = (eq_max - eq_min) * 0.1 if eq_max != eq_min else 1.0
        fig_equity.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            height=350,
            hovermode="x unified",
            yaxis_title="USD",
            yaxis=dict(range=[eq_min - eq_margin, eq_max + eq_margin]),
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_equity, use_container_width=True)

    # ============ TAB 2: デルタ分析 ============
    with tab2:
        # --- 2a. デルタ推移 ---
        st.subheader("デルタ推移")
        fig_delta = go.Figure()
        fig_delta.add_trace(
            go.Scatter(
                x=df_display["timestamp"],
                y=df_display["net_delta"],
                mode="lines",
                name="Net Delta (Smoothed)",
                line=dict(color="#636EFA", width=2),
            )
        )
        fig_delta.add_trace(
            go.Scatter(
                x=df_display["timestamp"],
                y=df_display["raw_net_delta"],
                mode="lines",
                name="Raw Net Delta",
                line=dict(color="#AB63FA", width=1, dash="dot"),
            )
        )

        fig_delta.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
        fig_delta.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            height=400,
            hovermode="x unified",
            yaxis_title="ETH",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_delta, use_container_width=True)

        # --- 2b. ETH価格 vs Net Delta (2軸) ---
        st.subheader("ETH価格 vs Net Delta")
        fig_dual = make_subplots(specs=[[{"secondary_y": True}]])
        fig_dual.add_trace(
            go.Scatter(
                x=df_display["timestamp"],
                y=df_display["eth_price"],
                mode="lines",
                name="ETH Price ($)",
                line=dict(color="#FFA15A", width=2),
            ),
            secondary_y=False,
        )
        fig_dual.add_trace(
            go.Scatter(
                x=df_display["timestamp"],
                y=df_display["net_delta"],
                mode="lines",
                name="Net Delta (ETH)",
                line=dict(color="#636EFA", width=1.5),
            ),
            secondary_y=True,
        )
        fig_dual.add_hline(
            y=0, line_dash="dash", line_color="gray", opacity=0.3, secondary_y=True
        )
        fig_dual.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            height=400,
            hovermode="x unified",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        fig_dual.update_yaxes(title_text="ETH Price ($)", secondary_y=False)
        fig_dual.update_yaxes(title_text="Net Delta (ETH)", secondary_y=True)
        st.plotly_chart(fig_dual, use_container_width=True)

    # ============ TAB 3: PnL分析 ============
    with tab3:
        # --- 3a. Funding Fees vs Accumulated PNL ---
        st.subheader("Funding Fees vs Accumulated PNL")
        fig_pnl = go.Figure()
        fig_pnl.add_trace(
            go.Scatter(
                x=df_display["timestamp"],
                y=df_display["funding_fees"],
                mode="lines",
                name="Funding Fees ($)",
                line=dict(color="#636EFA", width=1.5),
            )
        )
        fig_pnl.add_trace(
            go.Scatter(
                x=df_display["timestamp"],
                y=df_display["cum_pnl"],
                mode="lines",
                name="Accumulated PNL ($)",
                line=dict(color="#EF553B", width=1.5),
            )
        )
        fig_pnl.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
        fig_pnl.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            height=350,
            hovermode="x unified",
            yaxis_title="USD",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        st.plotly_chart(fig_pnl, use_container_width=True)

        # --- 3b. Step PnL バーチャート + Cum PnL オーバーレイ ---
        st.subheader("Step PnL (各区間の損益)")
        fig_step = make_subplots(specs=[[{"secondary_y": True}]])

        # Step PnL の色分け（正: 緑, 負: 赤）
        colors = ["#00CC96" if v >= 0 else "#EF553B" for v in df_display["step_pnl"]]

        fig_step.add_trace(
            go.Bar(
                x=df_display["timestamp"],
                y=df_display["step_pnl"],
                name="Step PnL ($)",
                marker_color=colors,
                opacity=0.6,
            ),
            secondary_y=False,
        )
        fig_step.add_trace(
            go.Scatter(
                x=df_display["timestamp"],
                y=df_display["cum_pnl"],
                mode="lines",
                name="Cumulative PnL ($)",
                line=dict(color="#AB63FA", width=2),
            ),
            secondary_y=True,
        )
        fig_step.update_layout(
            margin=dict(l=20, r=20, t=30, b=20),
            height=350,
            hovermode="x unified",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
        )
        fig_step.update_yaxes(title_text="Step PnL ($)", secondary_y=False)
        fig_step.update_yaxes(title_text="Cumulative PnL ($)", secondary_y=True)
        st.plotly_chart(fig_step, use_container_width=True)

    # ============ TAB 4: 統計サマリー ============
    with tab4:
        st.subheader("📊 パフォーマンスサマリー")

        stats = calc_performance_stats(df_filtered)
        if stats:
            col_s1, col_s2 = st.columns(2)
            with col_s1:
                st.markdown("#### 資産パフォーマンス")
                st.metric("期間開始 Equity", f"${stats['start_equity']:,.2f}")
                st.metric("現在 Equity", f"${stats['end_equity']:,.2f}")
                st.metric(
                    "期間リターン",
                    f"${stats['abs_return']:,.2f}",
                    delta=f"{stats['pct_return']:+.2f}%",
                )
                st.metric(
                    "最大ドローダウン",
                    f"{stats['max_drawdown_pct']:.2f}%",
                )
            with col_s2:
                st.markdown("#### PnL情報")
                st.metric("累積 PnL", f"${stats['cum_pnl']:,.2f}")
                st.metric("Funding Fees (最新)", f"${stats['funding_fees']:,.2f}")
                st.markdown("#### 期間")
                st.text(f"開始: {stats['start_time']}")
                st.text(f"終了: {stats['end_time']}")
                st.text(f"データ点数: {len(df_filtered)}")

        # 最新データのスナップショットテーブル
        st.subheader("📋 最新データスナップショット")
        latest_data = {
            "項目": [
                "Total Equity",
                "Uniswap Value",
                "Hyperliquid Value",
                "ETH Price",
                "LP Delta",
                "Net Delta",
                "Raw Net Delta",
                "Funding Fees",
                "Step PnL",
                "Cumulative PnL",
            ],
            "値": [
                f"${latest['total_equity']:,.2f}",
                f"${latest['uni_value']:,.2f}",
                f"${latest['hl_value']:,.2f}",
                f"${latest['eth_price']:,.2f}",
                f"{latest['lp_delta']:.6f} ETH",
                f"{latest['net_delta']:.6f} ETH",
                f"{latest['raw_net_delta']:.6f} ETH",
                f"${latest['funding_fees']:,.2f}",
                f"${latest['step_pnl']:,.4f}",
                f"${latest['cum_pnl']:,.2f}",
            ],
        }
        st.table(pd.DataFrame(latest_data))

    # ============ TAB 5: リバランス履歴 ============
    with tab5:
        st.subheader("🔄 リバランスイベント一覧")

        if rebalance_indices:
            rebalance_data = []
            for idx in rebalance_indices:
                row = df.iloc[idx]
                prev_row = df.iloc[idx - 1] if idx > 0 else row
                change = row["total_equity"] - prev_row["total_equity"]
                change_pct = (
                    (change / prev_row["total_equity"]) * 100
                    if prev_row["total_equity"] != 0
                    else 0
                )

                rebalance_data.append(
                    {
                        "タイムスタンプ": row["timestamp"],
                        "変動前 Equity ($)": f"{prev_row['total_equity']:,.2f}",
                        "変動後 Equity ($)": f"{row['total_equity']:,.2f}",
                        "変動額 ($)": f"{change:+,.2f}",
                        "変動率 (%)": f"{change_pct:+.2f}%",
                        "ETH Price ($)": f"{row['eth_price']:,.0f}",
                    }
                )

            df_rebalance = pd.DataFrame(rebalance_data)
            st.dataframe(df_rebalance, use_container_width=True)
        else:
            st.success("✅ リバランスによる不連続な変化は検出されませんでした。")

        # --- 生データ (最新120件) ---
        with st.expander("📂 生データログ (最新120件)"):
            st.dataframe(
                df.sort_values("timestamp", ascending=False).head(120),
                use_container_width=True,
            )

with col_logs:
    display_realtime_logs()

    # DynamoDB直近2日分データCSVダウンロード
    st.subheader("📥 データダウンロード")
    cutoff = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    df_recent = df[df["timestamp"] >= cutoff].sort_values("timestamp")
    if not df_recent.empty:
        csv_data = df_recent.to_csv(index=False).encode("utf-8")
        filename = f"delta_neutralize_2days_{datetime.utcnow().strftime('%Y%m%d')}.csv"
        st.download_button(
            label=f"📊 直近2日分データ (.csv) [{len(df_recent)}件]",
            data=csv_data,
            file_name=filename,
            mime="text/csv",
        )
    else:
        st.info("直近2日分のデータがありません。")

    # ログファイルダウンロード
    if os.path.exists(ORDER_LOG_PATH):
        with open(ORDER_LOG_PATH, "rb") as file:
            file_content = file.read()
        st.download_button(
            label="📥 注文ログ(.txt)を保存",
            data=file_content,
            file_name=ORDER_LOG_PATH,
            mime="text/plain",
        )
    else:
        st.warning("orderLog.log not found")
