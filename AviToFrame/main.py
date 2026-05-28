import cv2
import os

# 视频文件路径
video_path = 'tmp.avi'

# 保存目录
save_dir = 'frames'

# 差异阈值：
# 当前帧与上一帧的平均像素差大于该值时才保存
diff_threshold = 8

# 创建目录
os.makedirs(save_dir, exist_ok=True)

# 打开视频
cap = cv2.VideoCapture(video_path)

if not cap.isOpened():
    print("无法打开视频文件")
    exit()

frame_count = 0      # 视频总帧计数
save_count = 0       # 实际保存计数
prev_frame = None    # 上一帧

while True:
    success, frame = cap.read()

    if not success:
        break

    frame_count += 1

    should_save = False

    # 第一帧默认保存，后续帧通过像素差异判断是否保存
    if prev_frame is None:
        should_save = True
    else:
        diff = cv2.absdiff(prev_frame, frame)
        mean_diff = diff.mean()
        should_save = mean_diff >= diff_threshold

    if should_save:
        save_count += 1

        frame_path = os.path.join(
            save_dir,
            f'frame_{save_count:04d}.jpg'
        )

        cv2.imwrite(frame_path, frame)

        print(f'已保存: {frame_path}')
        if prev_frame is not None:
            print(f'与上一帧平均差异: {mean_diff:.2f}')

    prev_frame = frame.copy()

    # 显示视频
    cv2.imshow('Frame', frame)

    # 按 q 退出
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()

print(f'视频总帧数: {frame_count}')
print(f'实际保存帧数: {save_count}')
print(f'保存阈值: {diff_threshold}')
