import os
import time

import cv2
import numpy as np


# 视频文件路径
video_path = 'tmp.avi'

# 输出目录
save_dir = 'frames_fast_absdiff'

# 平均像素差阈值
diff_threshold = 6.0

# JPEG 质量，越低保存越快、文件越小
jpeg_quality = 85


os.makedirs(save_dir, exist_ok=True)

cap = cv2.VideoCapture(video_path)

if not cap.isOpened():
    print("无法打开视频文件")
    raise SystemExit(1)


frame_count = 0
save_count = 0
prev_gray = None

start_time = time.time()

while True:
    success, frame = cap.read()
    if not success:
        break

    frame_count += 1

    # 先转灰度，减少 3 通道比较开销
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    should_save = False

    if prev_gray is None:
        should_save = True
    else:
        diff = cv2.absdiff(prev_gray, gray)
        mean_diff = float(np.mean(diff))
        should_save = mean_diff >= diff_threshold

    if should_save:
        save_count += 1
        frame_path = os.path.join(save_dir, f'frame_{save_count:04d}.jpg')
        cv2.imwrite(frame_path, frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])

    prev_gray = gray

cap.release()

elapsed = time.time() - start_time
fps = frame_count / elapsed if elapsed > 0 else 0.0

print(f'视频总帧数: {frame_count}')
print(f'实际保存帧数: {save_count}')
print(f'平均差阈值: {diff_threshold}')
print(f'JPEG 质量: {jpeg_quality}')
print(f'总耗时: {elapsed:.3f} 秒')
print(f'处理速度: {fps:.2f} 帧/秒')
