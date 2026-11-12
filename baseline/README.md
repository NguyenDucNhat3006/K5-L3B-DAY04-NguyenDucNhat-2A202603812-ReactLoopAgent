# Bản trước cải tiến

`agent.py` trong thư mục này là bản Bài 1–2, trước khi thêm validation nguồn ở Bài 4.
Giữ bản này để có thể chạy lại phép so sánh mà không phải thay thế file agent chính.

Từ thư mục gốc của repo:

```powershell
python baseline/evaluate.py --workers 1 --output eval_before.json
python evals/run_evals.py --workers 1 --output eval_after.json
```

Cả hai lệnh tự đọc `.env` và dùng cùng provider, agent model, judge model, snapshot,
dataset và max steps mặc định (8). `evaluate.py` chỉ chọn hàm `run_agent` của bản
baseline; vẫn sử dụng runner, cases và grading gốc. Không chỉnh sửa kết quả eval.

Bản baseline cố ý chưa đạt hai tests kiểm tra thiếu URL nguồn trong
`tests/test_validation.py`; bản chính phải đạt toàn bộ tests.
