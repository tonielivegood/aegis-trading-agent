# Bộ lọc chống rug cho copy_trade — thiết kế

**Ngày:** 2026-07-29
**Trạng thái:** Đã duyệt qua brainstorm với anh TONiE (tất cả 4 phần), chờ review file cuối.

## Bối cảnh — vì sao cần

Đang tìm cách bắt được token BSC tăng nhanh (JensenHuang 50x/22h, PIG 17x/3h, CZ
390x/48h — xem `gem-wallets-v2-rebuild-handoff.md` memory) bằng một đường vào lệnh
mới (volume-spike, thiết kế riêng, sau việc này). Trước khi mở thêm đường vào lệnh
sớm hơn — vốn dĩ rủi ro cao hơn — anh TONiE yêu cầu củng cố bộ lọc chống rug trước.

Kiểm tra sống trên PIG (`0x40d46ecd7a40bef5311077aab840dcd23a123564`, một trong
những token vừa phân tích) qua GoPlus `token_security` API: **`is_mintable = 1`**
— chủ hợp đồng có thể tự in thêm token bất cứ lúc nào. Không phải honeypot, không
thuế cao, thanh khoản đủ — nhưng đây là một cửa rug thật đang mở mà
`passes_safety_check` (gate hiện tại, `binance_web3.py`) không hề kiểm tra: nó chỉ
lấy 2 trong ~20 trường hữu ích mà GoPlus đã trả về (`buy_tax`, `sell_tax` qua
`get_taxes()` trong `prices.py`), bỏ phí toàn bộ dữ liệu mint/ownership/proxy.

## Phần 1 — Kiến trúc: tách riêng, không đụng `passes_safety_check`

**Quan trọng: KHÔNG sửa `passes_safety_check` dùng chung.** Hàm này được gọi từ cả
copy_trade (`trade_engine.py:140`) VÀ Aegis/W3W (`agent_loop.py:362`, hệ trade rổ
147 token CMC-listed — `src/agent/data/eligible_tokens.json`, gồm ETH/USDT/USDC...).

Kiểm chứng trên chính token ETH-wrap-BSC mà Aegis đang trade
(`0x2170ed0880ac9a755fd29b2688956bd959f933f8`): GoPlus cũng trả về
**`is_mintable = 1`** — bình thường vì đây là cầu nối custodial của Binance, không
phải rug. Nếu chặn cứng `is_mintable` trong hàm dùng chung, Aegis sẽ không bao giờ
mua được ETH nữa. → Bằng chứng này đảo ngược lựa chọn ban đầu, đã trình lại và anh
TONiE xác nhận tách riêng.

**Quyết định:** hàm mới `passes_rug_check(token_address: str) -> tuple[bool, str]`
trong file mới `src/agent/copy_trade/rug_check.py`. Gọi trong
`TradeEngine.open_cluster_position` (`trade_engine.py`), ngay sau lệnh gọi
`passes_safety_check` hiện có (dòng 140), cùng kiểu xử lý khi fail: giải phóng
budget, log `cluster_buy_skipped_rug`, ghi signal `skipped_rug` — giống hệt cách
`skipped_safety` đang được xử lý.

**Chỉ chặn ở bước mua cuối cùng, không chặn lúc arm dossier.** Token bị rug flag
vẫn được arm/theo dõi bình thường (không tốn thêm lệnh gọi GoPlus lúc arm, không
đổi hành vi watchlist) — chỉ bị chặn đúng khoảnh khắc tiền thật sắp rời ví.

## Phần 2 — 7 cờ chặn cứng (dữ liệu GoPlus `token_security`)

Kiểm chứng sống trên PIG: cả 7 trường dưới đây đều là boolean 0/1 ổn định, có mặt
đầy đủ ngay cả với token 1 ngày tuổi — không có vấn đề dữ liệu thiếu.

| Cờ GoPlus | Ý nghĩa | Vì sao chặn |
|---|---|---|
| `is_mintable` | Chủ hợp đồng in thêm token được | In vô hạn rồi xả |
| `can_take_back_ownership` | "Đã renounce" giả — chủ lấy lại quyền được | Renounce giả là chiêu lừa kinh điển |
| `hidden_owner` | Địa chỉ chủ bị giấu | Không xác minh được gì cả |
| `owner_change_balance` | Chủ sửa được số dư ví bất kỳ | Rug tuyệt đối |
| `transfer_pausable` | Chủ tạm dừng giao dịch được | "Honeypot mềm" — mua được, chủ muốn là không bán được |
| `slippage_modifiable` | Chủ đổi thuế sau khi deploy | Có thể nâng thuế bán lên 100% sau khi đã mua |
| `is_proxy` | Hợp đồng có thể nâng cấp | Chủ đổi toàn bộ logic bất cứ lúc nào |

**Cố tình KHÔNG thêm:**
- `is_honeypot` — đã có ở `passes_safety_check` từ nguồn khác (aggregator quote),
  không trùng lặp thêm.
- `creator_percent`/`owner_percent` — đã trùng với gate `whale_risk`
  (`top_pct`/`top5_pct`) trong `phase2_score` (`watchlist.py`).
- `lp_holder_count`/trạng thái khoá LP — kiểm chứng trên PIG: cả hai đều `None`.
  Token vừa ra mắt (đúng loại copy_trade đang săn) thường chưa có dữ liệu này.
  Chặn cứng theo dữ liệu hay thiếu sẽ chặn nhầm phần lớn token hợp lệ. Để dành
  cho vòng sau, sau khi biết tỉ lệ null thực tế trên nhiều token hơn.

## Phần 3 — Xử lý lỗi: fail closed

Theo đúng triết lý đã có ở `passes_safety_check` (fail closed: lỗi hoặc thiếu dữ
liệu → không cho qua, không phải mặc định pass):

- GoPlus lỗi, timeout, hoặc không trả về record cho token này → **chặn**.
- Có record nhưng một trong 7 cờ bị thiếu trong JSON → coi là `0` (an toàn) — vì
  đã kiểm chứng GoPlus luôn trả đủ 7 trường boolean này kể cả với token rất mới;
  trường hợp thiếu chỉ xảy ra khi cả record lỗi, đã bị chặn ở bước trên.

## Phần 4 — Kiểm thử

3 nhóm test theo chuẩn TDD của repo (`monkeypatch` lên `requests.get`, theo mẫu
`test_dexscreener_pair_*` trong `tests/test_build_bsc_smart_wallets.py`):

1. Cả 7 cờ đều `0` → pass.
2. Từng cờ riêng lẻ bật `1` → chặn, đúng lý do trả về (7 case).
3. GoPlus lỗi/timeout/không có record → chặn (fail closed), không phải pass.

Không cần test tích hợp gọi GoPlus thật trong suite (giống `get_taxes`/
`get_holder_stats` hiện tại) — đã verify thủ công một lần bằng dữ liệu thật (PIG +
ETH) trong lúc thiết kế.

## Việc KHÔNG làm ở đây (ngoài phạm vi)

- Không sửa `passes_safety_check` hay bất kỳ code Aegis/W3W nào.
- Không thêm gate LP-lock (dữ liệu chưa đủ tin cậy — xem Phần 2).
- Không đổi hành vi arm/watchlist — chỉ chặn ở bước mua cuối.
- Đường vào lệnh sớm hơn (volume-spike) là thiết kế riêng, làm sau việc này, theo
  đúng thứ tự anh TONiE yêu cầu.
