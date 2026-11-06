# Bot tuất - Zoohoyo Status (Railway)
Bot Discord tự cập nhật bảng Server Status mỗi 10 phút.
## Deploy trên Railway
1. Tạo repo GitHub mới, upload 4 file này: bot.py, requirements.txt, board.json, Procfile
2. Vào railway.app > New Project > Deploy from GitHub repo > chọn repo này
3. Vào Variables, thêm: DISCORD_TOKEN = token bot (tự nhập, không gửi cho ai)
4. Deploy xong, bot sẽ tự chạy và cập nhật bảng đã có sẵn ở #nhắc-nhở-daily
