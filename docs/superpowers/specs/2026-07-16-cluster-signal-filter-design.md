# Cluster Signal Filter v1 — copy-trade buy/sell convergence gate

**Ngày:** 2026-07-16
**Trạng thái:** Đã duyệt bởi user, chờ viết implementation plan.

## Bối cảnh

Copy-trade gem-hunter (deploy 2026-07-15, xem
`docs/superpowers/specs/2026-07-15-copy-trade-gem-hunter-design.md`) hiện mua/bán ngay
khi **bất kỳ một** trong 10 ví GMGN smart-money mua/bán một token — mirror 1:1.

User chỉ ra một hổng chiến lược thật: nhãn "smart_degen" của GMGN là thông tin công
khai, ai dùng GMGN cũng thấy đúng 10 ví đó — không phải lợi thế độc quyền. Với chu kỳ
quét 30s (cộng độ trễ gắn nhãn của chính GMGN), bot luôn vào sau nhiều người copy khác
đã vào cùng ví đó rồi — mirror mù 1 ví không phải một edge, mà là vị trí bất lợi.

Cụm ví theo dõi cũng đã được thu gọn (2026-07-16): bỏ 5 ví cluster cuộc thi BNB Hack
(hết hoạt động/sai bản chất tín hiệu), chỉ còn giữ đúng 10 ví `GMGN_SMART_1..10`.

## Mục tiêu

Thêm một lớp lọc tự xây (không dựa hoàn toàn vào nhãn GMGN) phía trước quyết định mua:
chỉ mua khi **≥3 ví khác nhau trong 10 ví theo dõi cùng mua 1 token trong vòng 15
phút** — tín hiệu hội tụ (convergence/cluster signal) mạnh hơn hẳn một ví đơn lẻ hành
động. Đây là v1 — cố tình giữ đơn giản, mở rộng thêm sau (ví dụ: trọng số theo track
record của từng ví, ngưỡng N linh hoạt theo thanh khoản token...).

## Ngoài phạm vi (non-goals)

- Không thay đổi rào an toàn honeypot/thuế/thanh khoản/price-impact đã có
  (`binance_web3.passes_safety_check`) — lớp hội tụ này là một cổng MỚI, chạy TRƯỚC
  cổng an toàn, không thay thế.
- Không đổi kích thước lệnh ($1.50/slice) hay tổng ngân sách ($15.39).
- Không xây hệ thống chấm điểm/trọng số ví (v2+, chưa bàn ở đây).
- Không đổi self-custody signing hay execution backend.

## Kiến trúc

### 1. Bộ đệm tín hiệu mua hội tụ — chỉ trong RAM

**File mới:** `src/agent/copy_trade/cluster_signal.py`

`ClusterBuySignalTracker` — ghi nhận mỗi lần một ví MUA một token (`token_address`,
`wallet`, `timestamp`), giữ trong bộ nhớ tiến trình. Khi có tín hiệu mua mới cho một
token, kiểm tra: trong cửa sổ 15 phút gần nhất tính từ tín hiệu hiện tại, có bao nhiêu
**ví khác nhau** đã mua token này? Đủ `min_wallets` (mặc định 3, đọc từ
`copy_settings` trong `config.json` để dễ chỉnh sau) → trả về danh sách chính xác các
ví đã hội tụ; chưa đủ → trả `None`.

Dữ liệu này **cố tình không lưu đĩa** — nếu process restart giữa lúc đang tích lũy
tín hiệu (chưa đủ 3 ví), mất là chấp nhận được vì chưa có tiền đặt cược; khác hẳn vị
thế thật (đã mua) — thứ luôn phải lưu đĩa theo nguyên tắc đã áp dụng từ vụ mất-dấu-
vị-thế trước đây.

Bộ đệm tự dọn (loại bỏ quan sát cũ hơn cửa sổ 15 phút) mỗi lần được truy vấn — không
cần tiến trình dọn dẹp riêng.

### 2. Chặn mua trùng token đang giữ

Trước khi cho phép một lệnh mua do hội tụ kích hoạt, kiểm tra: token này đã có vị thế
copy-trade đang mở chưa (bất kể ví nào đã kích hoạt lúc trước)? Có rồi → bỏ qua, tránh
mua chồng mỗi khi có thêm ví thứ 4, 5... cũng mua cùng token đó. Cần thêm
`PositionStore.find_by_token(token_address) -> CopyPosition | None` (tìm theo token,
không cần khớp ví) — bổ sung, không thay thế `find(token_address, source_wallet)` sẵn
có (vẫn cần cho logic khác).

**Giả định rõ ràng cho v1:** không bao giờ giữ đồng thời 2 vị thế trên cùng 1 token.

### 3. Gắn "cụm ví" vào vị thế đã mua

Mở rộng `CopyPosition` (`positions.py`, đã lưu đĩa) thêm 2 trường:
- `cluster_wallets: list[str]` — chính xác các ví đã kích hoạt lệnh mua (ghi 1 lần
  lúc mở vị thế, không đổi sau đó).
- `exited_by: list[str]` — các ví trong `cluster_wallets` đã bán token này, cập nhật
  dần mỗi khi có tín hiệu bán khớp. Mặc định rỗng.

Cả 2 trường lưu đĩa cùng vị thế (đồng bộ với cơ chế ghi nguyên tử đã có từ trước) —
sống sót qua restart, không lặp lại kiểu lỗi mất-dấu như trước.

### 4. Quy tắc thoát — "đa số bán thì bán"

Khi có tín hiệu BÁN từ ví W cho token T:
1. Tìm vị thế đang mở cho T qua `find_by_token`.
2. Nếu không có vị thế → bỏ qua (như hiện tại).
3. Nếu có: W có nằm trong `cluster_wallets` của vị thế đó không?
   - **Không** (ví ngoài cụm gốc bán token này) → bỏ qua, không tính.
   - **Có** → thêm W vào `exited_by` (nếu chưa có), lưu đĩa ngay.
4. Sau khi cập nhật: nếu `len(exited_by) > len(cluster_wallets) / 2` (đa số, vd. 2/3)
   → thực hiện bán thật (qua executor hiện có, đã có exit-failover từ trước), đóng vị
   thế, trả ngân sách. Chưa đủ đa số → tiếp tục giữ, chờ thêm tín hiệu bán.

### 5. Điểm nối vào `monitor.py`

Trong vòng quét (`run_scan()`), thay vì gọi `handle_alert` ngay cho mọi alert như hiện
tại:
- **Alert MUA:** đưa qua `ClusterBuySignalTracker` trước. Hội tụ đủ (và chưa có vị
  thế cho token đó) → gọi `handle_alert` thật (dùng dữ liệu token từ tín hiệu mua mới
  nhất, gắn thêm `cluster_wallets` vào `CopyPosition` khi mở). Chưa đủ → chỉ log
  "đang chờ xác nhận thêm", **vẫn gửi email thông báo** như bình thường (giữ khả năng
  quan sát), chỉ không thực thi mua.
- **Alert BÁN:** đưa qua logic đa số ở mục 4 thay vì gọi thẳng `_handle_sell`.

### 6. Không đổi

Rào an toàn (honeypot/thuế/thanh khoản/price-impact), kích thước lệnh, ngân sách
tổng, self-custody signing, exit-failover across backends — toàn bộ giữ nguyên,
lớp hội tụ chỉ là cổng lọc mới đứng trước.

## Testing

- Unit test `ClusterBuySignalTracker`: chưa đủ ví → `None`; đủ 3 ví khác nhau trong
  cửa sổ → trả đúng danh sách; tín hiệu ngoài cửa sổ 15 phút không được tính; cùng 1
  ví mua nhiều lần không được tính là nhiều ví khác nhau.
- Unit test `find_by_token`: tìm đúng vị thế theo token bất kể ví; trả `None` khi
  không có vị thế nào cho token đó.
- Unit test quy tắc đa số: ví ngoài cụm bán → không tính, không đóng vị thế; 1/3 ví
  trong cụm bán → chưa đóng; 2/3 → đóng thật, đúng thứ tự đóng-vị-thế-rồi-trả-ngân-
  sách; test riêng cụm có số lẻ khác 3 (vd. nếu sau này N cấu hình khác 3) để công
  thức "quá nửa" đúng tổng quát, không hardcode `>= 2`.
- Test tích hợp `monitor.py`: 3 alert mua liên tiếp từ 3 ví khác nhau cùng token trong
  15 phút → đúng 1 lệnh mua thật được gọi (không phải 3); alert mua thứ 4 cùng token
  sau đó → không mua thêm (đã có vị thế).

## Rủi ro đã biết / chấp nhận

- Siết chặt hơn nghĩa là **ít tín hiệu hơn hẳn** — ngân sách $15.39 có thể không được
  dùng hết trong thời gian dài nếu không đủ 3 ví hội tụ. Đây là đánh đổi có chủ đích
  (chất lượng tín hiệu hơn số lượng), không phải lỗi.
- Quy tắc "đa số thoát" có thể giữ vị thế lâu hơn "bán ngay khi 1 ví bán" nếu cụm 3 ví
  không đồng thuận bán cùng lúc — chấp nhận được vì đây là quyết định người dùng chọn
  rõ ràng (ưu tiên đồng thuận hơn thoát nhanh).
- v1 không có trọng số/track-record theo từng ví — mọi ví trong 10 ví được đối xử
  ngang nhau. Để lại cho vòng tư duy tiếp theo (đã được user xác nhận là hướng đi sau
  v1).
