# Copy-Trade Gem Hunter — thay thế chiến lược Aegis

**Ngày:** 2026-07-15
**Trạng thái:** Đã duyệt bởi user, chờ viết implementation plan.

## Bối cảnh

Bot Aegis (volume-breakout sniper) hiện đang chạy trên VPS đã mất ~54% equity (từ
$33.27 xuống $15.39) trong 2 tuần, một phần do lỗi vị thế "mồ côi" (`_discovered`
registry chỉ sống trong RAM, mất dấu khi process restart — xem
`data/runtime/trade_journal.jsonl` các entry 金狗/未来协议 không có exit khớp).

User quyết định **thay thế hoàn toàn** chiến lược này bằng một chiến lược đầu cơ cao
rủi ro: tự động mua/bán theo các ví "cá mập" có lịch sử trúng nhiều gem-coin trên BSC,
chấp nhận mất toàn bộ $15.39 còn lại như một canh bạc xác suất thấp/phần thưởng cao,
tách biệt khỏi triết lý quản trị rủi ro của Aegis.

Khám phá quan trọng trong lúc thiết kế: repo đã có sẵn một module chưa hoàn thiện,
**chưa từng được commit vào git**, tại `src/agent/copy_trade/monitor.py` — theo dõi
real-time swap của 8 ví cluster (các đội hạng #1-4 cuộc thi BNB Hack) qua Moralis API,
ghi alert + gửi email, nhưng phần tự động mua theo còn để `# TODO`. Module này đã chạy
liên tục từ 7/7 (PID 194294, systemd không quản lý) nhưng **100% request tới Moralis
trả về 401 Unauthorized suốt 8 ngày** — hoàn toàn vô dụng, không ai phát hiện.

Xác minh on-chain (2026-07-15): ví `MAIN_TRADE` có nonce hiện tại = 186, y hệt số ghi
nhận từ config lúc 4/7 → 11 ngày qua không có giao dịch mới, xác nhận ví cluster cuộc
thi đã ngừng hoạt động. Kể cả lúc còn sống, các ví này trade theo luật chấm điểm cuộc
thi (AAVE, ETH, LINK, USDT, BTCB...) — token lớn đã niêm yết, sai bản chất tín hiệu so
với mục tiêu "gem x1000". → cụm ví này bị hạ xuống nguồn phụ, không phải nguồn chính.

## Mục tiêu

Tự động mua theo và bán theo (mirror) các ví smart-money trên BSC có lịch sử trúng
nhiều gem-coin, dùng toàn bộ $15.39 còn lại, chấp nhận mất sạch. Không mục tiêu bảo
toàn vốn — đây là thử nghiệm đầu cơ, không phải nâng cấp chiến lược risk-managed.

## Ngoài phạm vi (non-goals)

- Không tự xây hệ thống chấm điểm ví từ đầu (dùng nhãn có sẵn của GMGN).
- Không đặt luật chốt lời/cắt lỗ riêng — thoát vị thế hoàn toàn theo tín hiệu bán của
  ví nguồn.
- Không chi thêm ngân sách hạ tầng ngoài $15.39 vốn trade (free tier only).
- Không giữ song song bot Aegis — thay thế hoàn toàn, dừng `agent.service`.

## Kiến trúc

### 1. Nguồn tín hiệu ví

- **Nguồn chính:** ví "Smart Money" gắn nhãn sẵn của GMGN cho BSC (free tier, tối đa
  10 ví). Cần một bước xác minh kỹ thuật đầu tiên (spike): endpoint GMGN OpenAPI chính
  xác để lấy danh sách này (chưa xác nhận 100% chi tiết auth/rate-limit ở thời điểm
  viết spec) — risk mở, xử lý ở task đầu tiên của implementation plan.
- **Nguồn phụ (giữ nguyên, không ưu tiên):** 8 ví cluster cuộc thi đã có trong
  `data/copy_trade/config.json` — giữ theo dõi (miễn phí, vô hại) nhưng không phải
  tín hiệu chính, vì đã xác nhận phần lớn ngừng hoạt động/sai bản chất tín hiệu.
- Merge cả hai nguồn vào `target_wallets`, refresh danh sách GMGN định kỳ (vd. mỗi
  ngày, vì bảng xếp hạng GMGN đổi theo thời gian).

### 2. Vòng quét phát hiện (nâng cấp `monitor.py` có sẵn)

- Giữ nguyên cơ chế poll Moralis mỗi 30s + dedup qua `processed_txs`.
- **Bắt buộc trước tiên:** thay `MORALIS_API_KEY` bằng key mới hợp lệ (key hiện tại
  401 toàn bộ, im lặng suốt 8 ngày qua — bài học trực tiếp cần vá).
- Thêm cảnh báo lỗi liên tục: N lần quét lỗi liên tiếp (vd. 10 lần ~5 phút) → gửi 1
  email cảnh báo qua `send_email_alert` đã có sẵn, tránh lặp lại kiểu hỏng-âm-thầm.

### 3. Ngân sách & kích thước lệnh (module mới, nhỏ, pure function — dễ unit test)

- Tổng ngân sách: $15.39 (số dư hiện tại của ví contest).
- Mỗi lệnh copy-buy: khoản cố định ~$1.0–1.5 (~10 lượt cược khả dụng).
- Khi ngân sách khả dụng (chưa phân bổ) không đủ cho 1 lệnh mới → bỏ qua tín hiệu mua
  mới, nhưng vẫn tiếp tục theo dõi để bán các vị thế đang giữ.

### 4. Tự động mua theo

- Khi phát hiện swap "mua" (stable/native → token mới) từ ví theo dõi và còn ngân
  sách: đăng ký token qua `token_list.register_discovered()` (tái dùng pattern hiện
  có), chạy qua **rào an toàn tối thiểu** (kiểm tra honeypot/thuế — tái dùng check đã
  có trong pipeline discovered-token, không viết mới), rồi gọi
  `best_execution.py.rank_backends()` + executor thắng cuộc để mua với số tiền theo
  mục 3.

### 5. Tự động bán theo (mirror-exit — logic mới)

- Khi ví nguồn bán một token mà bot đang giữ (đã copy-buy trước đó) → bán toàn bộ vị
  thế đó ngay theo cùng executor, không có luật chốt lời/cắt lỗ riêng nào khác.
- Nếu nhiều ví nguồn từng mua cùng 1 token, chỉ bán khi ví đã kích hoạt lệnh mua ban
  đầu cho vị thế đó bán ra (position gắn với ví nguồn cụ thể, không gộp).

### 6. Lưu vị thế bền vững — vá đúng root cause vừa tìm ra

- Ghi mọi vị thế copy-trade ra `data/copy_trade/positions.json` **ngay khi mua**, nạp
  lại từ đĩa khi process khởi động. Đây là điều kiện bắt buộc — khác với lỗi
  `_discovered` (chỉ sống trong RAM) đã làm mất dấu 2 vị thế 金狗/未来协议 suốt 9 ngày.
- Dựng `copy_trade.monitor` thành một `systemd` unit có giám sát (`Restart=always`),
  thay vì process rời rạc như hiện tại (không ai tự khởi động lại nếu crash).
  **Đã có sẵn, chưa từng cài đặt:** `deploy/copy-trade.service` (untracked, đúng cấu
  hình cần — `Restart=always`, log ra `logs/copy_trade.log`) — implementation chỉ cần
  copy lên VPS + `systemctl enable --now`, không cần viết lại.

### 7. Thay thế bot cũ

- Dừng và disable `agent.service` (Aegis) trên VPS.
- `copy_trade` trở thành chiến lược sống duy nhất, dashboard/status.json chuyển sang
  phản ánh trạng thái copy-trade thay vì Aegis.

## Testing

- Unit test: logic chia ngân sách (đủ/không đủ tiền cho lệnh mới, nhiều lệnh liên
  tiếp cạn ngân sách đúng lúc).
- Unit test: logic khớp lệnh bán (given: alert bán từ ví nguồn + danh sách vị thế
  đang giữ → xác định đúng vị thế cần thoát, không nhầm giữa nhiều ví nguồn).
- Test có mock response Moralis: giả lập chuỗi swap mua→bán, xác nhận gọi đúng
  executor với đúng token/số tiền ở mỗi bước.
- Kịch bản test restart: ghi vị thế → giả lập restart process → xác nhận vị thế được
  nạp lại đầy đủ từ `positions.json`, không bị mất dấu như lỗi cũ.

## Rủi ro đã biết / chấp nhận

- Đây là chiến lược đầu cơ xác suất thấp/thưởng cao — khả năng mất sạch $15.39 là kết
  quả được chấp nhận trước, không phải thất bại kỹ thuật.
- Free tier GMGN có độ trễ (không phải mili-giây) — bot sẽ luôn vào sau ví gốc một
  khoảng thời gian, không phải lợi thế tốc độ tuyệt đối.
- Chi tiết kỹ thuật chính xác của GMGN OpenAPI (endpoint, auth, rate limit cho free
  tier) chưa được xác minh trực tiếp — task đầu tiên của implementation phải xác minh
  trước khi code phần còn lại.
