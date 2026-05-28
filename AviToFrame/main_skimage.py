import os

import cv2
from skimage.metrics import structural_similarity as ssim


# 视频文件路径
video_path = 'tmp.avi'

# 保存目录
save_dir = 'frames_skimage'

# SSIM 阈值：
# SSIM 越接近 1，说明两帧越相似
# 当相似度低于该阈值时保存当前帧
ssim_threshold = 0.88


os.makedirs(save_dir, exist_ok=True)

cap = cv2.VideoCapture(video_path)

if not cap.isOpened():
    print("无法打开视频文件")
    exit()


frame_count = 0
save_count = 0
prev_gray = None

while True:
    success, frame = cap.read()
    if not success:
        break

    frame_count += 1
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    should_save = False
    score = None

    # 第一帧默认保存，后续帧用 SSIM 比较结构相似度
    if prev_gray is None:
        should_save = True
    else:
        score = ssim(prev_gray, gray)
        should_save = score < ssim_threshold

    if should_save:
        save_count += 1
        frame_path = os.path.join(save_dir, f'frame_{save_count:04d}.jpg')
        cv2.imwrite(frame_path, frame)
        print(f'已保存: {frame_path}')
        if score is not None:
            print(f'SSIM: {score:.4f}')

    prev_gray = gray.copy()

    cv2.imshow('Frame', frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()

print(f'视频总帧数: {frame_count}')
print(f'实际保存帧数: {save_count}')
print(f'SSIM 阈值: {ssim_threshold}')
