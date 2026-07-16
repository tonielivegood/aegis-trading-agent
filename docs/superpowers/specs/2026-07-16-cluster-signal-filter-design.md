# BSC Smart-Money Cluster + Shadow-Mode — thiết kế v2

**Ngày:** 2026-07-16 (v2, thay toàn bộ v1 cùng ngày — xem git history cho bản cũ)
**Trạng thái:** Đã duyệt qua brainstorm với user, chờ user review file này lần cuối.

## Bối cảnh — vì sao v1 bị thay

v1 chỉ thêm cổng cluster (≥3 ví) lên trên 10 ví GMGN hiện có. Brainstorm 16/7 phát hiện
các vấn đề khiến v1 chết từ thiết kế:

1. **Nguồn ví sai:** 10 ví `GMGN_SMART_1..10` chủ yếu hoạt động trên Solana/Robinhood,
   hoạt động BSC thưa — luật "≥3 ví cùng mua trong 15 phút" gần như không bao giờ
   kích hoạt. Phải xây danh sách ~50 ví smart-money **thuần BSC** trước (Phần 1).
2. **Moralis không gánh nổi:** 10 ví × poll 30s đã đốt sạch quota free-plan trong
   một phần ngày (bot mù từ đó đến reset, lỗi 401 quota bị alert nhầm thành "key
   invalid"). 50 ví là bất khả thi → đổi nguồn dữ liệu sang BSC RPC công khai.
3. **Chiều thoát không bán nổi token có thuế:** executor dùng
   `swapExactTokensForTokens` thường — token thuế 3-5% (đa số meme Tàu) revert
   100% khi bán, bất kể slippage (vi phạm K-check vì pair nhận thiếu). Đã xác nhận
   bằng vụ 金狗/未来协议 kẹt từ 5-6/7, gỡ thành công 16/7 bằng
   `swapExactTokensForTokensSupportingFeeOnTransferTokens`.
4. **Backlog replay:** khởi động với `state.json` rỗng → coi 25 giao dịch lịch sử
   mỗi ví là tín hiệu sống. 16/7 lúc 01:45 một instance DRY_RUN đã "mua" 9 vị thế ma
   từ backlog của 1 ví trong 52 giây — nếu là live thì mất $13.50 vào lệnh stale.
5. **Paper/real lẫn lộn:** vị thế simulated ghi chung `positions.json` với vị thế
   thật, không có cờ phân biệt — gây nhầm lẫn ngân sách và mất công điều tra.

**Trạng thái hiện tại (16/7, sau dọn dẹp):** `auto_execute=false`, `positions.json`
rỗng (9 vị thế ma đã backup ở `positions.json.bak.phantom-20260716`), 2 token mồ côi
Aegis đã bán tay, ví sạch: **$16.14 USDT + ~0.0077 BNB (gas)**. Không có vị thế thật
nào cần chăm sóc — mọi thứ dưới đây build trên nền trắng.

## Mục tiêu

- **Phần 1:** danh sách ~50 ví smart-money BSC tự xây (không dựa mù vào nhãn GMGN).
- **Phần 2:** bot theo dõi 50 ví đó qua BSC RPC, chỉ coi là tín hiệu vào khi **≥3 ví
  khác nhau mua cùng token trong 15 phút**, thoát khi **≥2 ví trong cụm gốc** xả
  token hoặc giá -70%; chạy **shadow-mode (paper)** cho đến khi đủ **≥10 sự kiện
  cluster** và user duyệt go-live với tiền thật ($3/lệnh, 5 slot, cap $16.14).

## Ngoài phạm vi (non-goals)

- Không trọng số/track-record theo ví (v3+; khung chấm điểm Phần 1 là chỗ gắn sau).
- Không đổi self-custody signing; không thêm chain ngoài BSC.
- Không tự động refresh danh sách ví theo lịch (chạy tay ~2-4 tuần/lần).
- Không take-profit chủ động — thoát chỉ theo cụm hoặc van -70%.

---

## Phần 1 — Xây danh sách 50 ví smart-money BSC

**Hình thức:** script chạy tay trên máy dev Windows (gmgn-cli + key đã config local),
KHÔNG chạy trên VPS. Output: `data/copy_trade/wallets.json`.

### Nguồn seed (2 nhánh)

1. **GMGN BSC leaderboard:** kéo top ~200 ví theo PnL 7d/30d qua gmgn-cli
   (`--chain bsc`), giữ lại winrate/PnL/tag làm feature đầu vào.
2. **Early-buyer scan (phần "của mình"):** user + assistant chọn 5-10 token BSC đã
   chạy mạnh gần đây (từ DexScreener gainers, duyệt tay từng token). Với mỗi token:
   quét `Transfer` logs qua BSC RPC công khai từ block tạo pair, lấy các ví nhận
   token trong giai đoạn sớm (trước khi giá chạy). Ví xuất hiện sớm ở **≥2 token
   thắng khác nhau** → candidate.

### Bộ lọc + chấm điểm chung

Áp cho mọi candidate từ cả 2 nhánh:

- **Loại thẳng:** là contract (`eth_getCode` ≠ rỗng); >100 tx/ngày (bot MEV/sniper);
  không có tx BSC nào trong 7 ngày gần nhất (ví chết/nguội).
- **Điểm cộng:** số token-thắng mua sớm (nhánh 2); PnL/winrate GMGN (nhánh 1);
  xuất hiện ở CẢ 2 nhánh.
- Lấy top 50 theo điểm. Ngưỡng/trọng số cụ thể để implementation plan chốt —
  nguyên tắc: đơn giản, giải thích được, in ra bảng điểm cho user duyệt.

### Output format

`wallets.json`: mảng `{address, label, score, sources: [gmgn|early_buyer|both],
added_at, notes}`. Monitor Phần 2 chỉ đọc file này (bỏ `target_wallets` trong
`config.json`). Refresh = chạy lại script → diff → user duyệt → scp lên VPS.

---

## Phần 2 — Nguồn dữ liệu RPC + cluster + shadow-mode

### 1. Nguồn tín hiệu: BSC RPC `eth_getLogs` (file mới `chain_events.py`)

Bỏ hẳn Moralis. Mỗi vòng quét (30-60s, đọc config):

- Quét block mới kể từ block đã xử lý cuối: `eth_getLogs` topic `Transfer`, lọc
  `to` ∈ 50 ví (tín hiệu MUA tiềm năng) và `from` ∈ 50 ví (tín hiệu XẢ).
  Topic filter nhận mảng OR — 1-2 call/vòng cho cả 50 ví.
- **Xác nhận MUA:** với mỗi candidate nhận-token, fetch receipt của tx đó và yêu cầu
  có event `Swap` (V2/V3) trong cùng tx — loại airdrop/transfer thường. Khối lượng
  candidate thấp (50 ví smart) nên chi phí receipt không đáng kể.
- **Tín hiệu XẢ (cho chiều thoát):** token RỜI ví cụm là đủ — không cần event Swap,
  không cần parse lệnh bán. Bán qua multi-hop, gửi sang ví khác, gửi lên sàn... đều
  bắt được. Đây là bản vá tận gốc cho lỗ hổng "parser bỏ sót lệnh bán" của v1.
- Nhiều RPC endpoint fallback (dataseed chính thức + 2-3 public khác), xoay vòng khi
  lỗi. Miễn phí, không quota.
- **Chống backlog-replay:** khi start, ghi `start_block = block hiện tại` và chỉ xử
  lý sự kiện từ block đó trở đi. Không bao giờ hành động trên lịch sử.
- **Alert nguồn-dữ-liệu-chết:** N vòng quét liên tiếp mà mọi RPC đều lỗi → email
  (tổng quát hóa bài học Moralis-401-8-ngày và quota-block bị alert sai hôm nay).

### 2. Cluster gate (file mới `cluster_signal.py` — giữ từ v1)

`ClusterBuySignalTracker` RAM-only: ghi (token, wallet, timestamp) mỗi tín hiệu mua
đã xác nhận; đủ `min_wallets` (mặc định 3) ví **khác nhau** trong cửa sổ
`window_minutes` (mặc định 15) → trả danh sách ví cụm. Tự dọn quan sát cũ hơn cửa
sổ. Mất dữ liệu khi restart là chấp nhận được (chưa có tiền đặt cược). Chưa đủ
ngưỡng → chỉ log, KHÔNG email (50 ví BSC hoạt động dày — email mọi tín hiệu lẻ sẽ
thành spam; khác v1). Email chỉ dành cho: cluster kích hoạt, thoát vị thế, lỗi
nguồn dữ liệu.

### 3. Vị thế + chiều thoát

`CopyPosition` thêm: `cluster_wallets: list[str]`, `exited_by: list[str]`,
`entry_price_usd: float`, `simulated: bool`. Lưu đĩa nguyên tử như cũ;
`_build_runtime`/reconcile phải replay đủ các trường mới.

- **Chặn trùng:** `PositionStore.find_by_token(token_address)` — đã có vị thế mở
  (thật hay ảo) cho token → không mua thêm dù có ví thứ 4, 5 hội tụ.
- **Thoát theo cụm:** tín hiệu XẢ từ ví W cho token T đang giữ: W ∈ `cluster_wallets`
  → thêm vào `exited_by`, lưu ngay. `len(exited_by) >= exit_wallets` (mặc định 2)
  → bán toàn bộ.
- **Van khẩn cấp:** mỗi vòng quét, lấy giá các token đang giữ từ DexScreener API
  (free, không key). Giá ≤ entry × (1 − 0.70) → bán ngay bất kể cụm. Van là lớp
  chống-thảm-họa, không phải stop-loss chiến lược.
- **Fix bán token có thuế (bắt buộc):** executor chuyển sang
  `swapExactTokensForTokensSupportingFeeOnTransferTokens` cho CẢ hai chiều
  (tương thích ngược với token không thuế). Lượng token thực nhận sau mua ghi theo
  **delta `balanceOf`** trước/sau swap, không theo `expected_out_wei` (token thuế
  làm expected lệch ~tax%).

### 4. Shadow-mode (paper-trading đầy đủ)

Cờ `shadow_mode` trong `copy_settings` (mặc định `true` khi deploy). Cờ
`auto_execute` cũ bị XÓA — hai cờ chồng nhau sẽ mâu thuẫn; từ v2 chỉ còn một công
tắc duy nhất: `shadow_mode=true` → fill ảo, `false` → tiền thật.

- Toàn bộ pipeline trên chạy y hệt, nhưng fill ảo: giá DexScreener tại thời điểm
  trigger + mô hình phí (gas ước ~$0.10/leg + buy/sell tax đọc từ GoPlus qua
  safety-gate sẵn có + ~1% impact). Ghi vào **`shadow_positions.json` riêng**,
  `simulated=true` — không bao giờ chung file với vị thế thật.
- Mỗi record shadow ghi đủ để đánh giá chiến lược: giá lúc ví-1 mua, giá lúc cụm đủ
  3 ví (đo độ trễ-vào), thời điểm/giá/lý do thoát (cụm-xả | van-70 | đang mở).
- Email như live (đánh dấu [SHADOW]) để user theo dõi real-time.
- **Tiêu chí kết thúc:** đủ **≥10 sự kiện cluster** → assistant tổng hợp báo cáo
  PnL-sau-phí → user quyết go-live. Bot KHÔNG bao giờ tự lật cờ. Nếu ~3 tuần chưa
  đủ 10 sự kiện thì bản thân điều đó là kết luận (tín hiệu quá hiếm → xét lại
  ngưỡng 3-ví hoặc danh sách ví).
- Go-live = `shadow_mode: false` + safety-gate/executor thật như hiện có.

### 5. Sizing & ngân sách (khi go-live)

`slice_usd: 3.0`, `total_budget_usd: 16.14` (xác minh số dư USDT thật tại thời điểm
go-live), tối đa 5 vị thế đồng thời. Lý do $3 thay $1.50: phí cố định (gas 2 chiều +
thuế 3-5%/chiều + slippage) trên lệnh $1.50 ăn 20-40% vị thế — $3 giảm một nửa tỷ lệ
cản. Báo cáo shadow phải in rõ ngưỡng hòa-vốn theo mô hình phí để kiểm chứng con số
này bằng dữ liệu thật.

### 6. Không đổi

Safety-gate honeypot/thuế/thanh khoản (`binance_web3.passes_safety_check`) vẫn chạy
trước mọi lệnh mua (thật lẫn ảo — ảo cũng ghi lại kết quả gate để báo cáo trung
thực); exit-failover qua mọi backend; systemd service; email notifier.

## Testing

- `chain_events`: phân loại đúng mua-có-Swap vs airdrop; xả nhận diện mọi kiểu
  token-rời-ví; không xử lý block < start_block; RPC lỗi → fallback endpoint.
- `cluster_signal`: như v1 (đủ/thiếu ví, cửa sổ trượt, dedup cùng ví).
- Thoát: ví ngoài cụm xả → không tính; 1 ví cụm xả → giữ; 2 ví → bán;
  van -70% bán bất kể cụm; thứ tự đóng-vị-thế-rồi-trả-ngân-sách.
- `find_by_token` chặn mua trùng.
- Executor: bán token thuế 4% thành công qua SupportingFee (mock); fill ghi theo
  balance-delta.
- Shadow: 3 alert mua 3 ví/15phút → đúng 1 vị thế ảo, 0 giao dịch thật khi
  `shadow_mode=true` (test này là chốt an toàn quan trọng nhất).
- Reconcile sau restart: vị thế (thật/ảo) nạp lại đủ `cluster_wallets`/`exited_by`/
  `entry_price_usd`, ngân sách chỉ tính vị thế `simulated=false`.

## Rủi ro đã biết / chấp nhận

- Cụm 3-ví có thể kích hoạt ĐÚNG LÚC token đã chạy mạnh (mua đỉnh sóng đầu) — đây
  chính là giả thuyết shadow-mode tồn tại để kiểm chứng, đo bằng chênh giá ví-1 →
  cụm-đủ trong báo cáo.
- Public RPC không SLA — nhiều fallback + alert; độ trễ quét 30-60s chấp nhận được
  với cửa sổ 15 phút.
- DexScreener không SLA — van -70% và giá shadow phụ thuộc; lỗi thì giữ nguyên trạng
  thái và alert, không đoán giá.
- Ví smart-money có thể xả qua đường không phát hiện được (CEX deposit qua internal
  transfer? — không: mọi chuyển ERC-20 đều emit Transfer; chỉ contract tự-hủy kiểu
  lạ mới thoát) — coi như đủ tốt cho v1.

## Trình tự triển khai

1. **Phần 1** (worktree/branch riêng, TDD như quy trình chuẩn): script + wallets.json
   → user duyệt danh sách 50 ví.
2. **Phần 2** (tiếp cùng branch hoặc branch mới): chain_events → cluster_signal →
   exit/executor fix → shadow-mode → deploy VPS với `shadow_mode=true` (cờ
   `auto_execute` bị gỡ khỏi config trong cùng thay đổi này).
3. Whole-branch review đa vòng trước merge (đã 2 lần chứng minh bắt bug Critical mà
   review từng task không thấy — đặc biệt soi lớp reconcile-sau-restart và chốt an
   toàn shadow-không-trade-thật).
4. Chờ ≥10 sự kiện → báo cáo → user quyết go-live.
