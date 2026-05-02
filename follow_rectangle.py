import time, os, sys, gc
from media.sensor import *
from media.display import *
from media.media import *
import cv_lite
from machine import UART, FPIOA

fpioa = FPIOA()
fpioa.set_function(11, fpioa.UART2_TXD)
fpioa.set_function(12, fpioa.UART2_RXD)
uart2 = UART(2, baudrate=115200)

cam_w = 640
cam_h = 480

ST7701_w = 800
ST7701_h = 480

display_x = (ST7701_w - cam_w) // 2
display_y = (ST7701_h - cam_h) // 2

screen_center_x = cam_w // 2 
screen_center_y = (cam_h // 2) + 9

sensor = None
#format: ID_addr, func,  dir, acce, acce,  vel,  vel, 同步,CheckSum
rx_order = [0x00, 0xF6, 0x01, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x6B]

def send_order(addr_, dir_, vel_):
    rx_order[0] = addr_
    rx_order[2] = dir_
    rx_order[5] = (vel_ // 256) % 256
    rx_order[6] = vel_ % 256
    uart2.write(bytes(rx_order))
    #print(bytes(rx_order))

try:
    sensor = Sensor(id=2, fps=90)
    sensor.reset()
    sensor.set_framesize(width=cam_w, height=cam_h)
    sensor.set_pixformat(Sensor.GRAYSCALE)

    Display.init(Display.ST7701, width=ST7701_w, height=ST7701_h, to_ide=True)
    MediaManager.init()
    sensor.run()

    gain = k_sensor_gain()
    gain.gain[0] = 20
    sensor.again(gain)

    clock = time.clock()

    # 1阶低通滤波器EMA初始化（整数，alpha = ALPHA_NUM / ALPHA_DEN）
    filtered_err_x = 0
    filtered_err_y = 0
    ALPHA_NUM = 7   # 新值权重，调小→滤波更强/响应更慢，调大→滤波更弱/响应更快
    ALPHA_DEN = 10  # 分母，保持10

    canny_thresh1      = 50
    canny_thresh2      = 150
    approx_epsilon     = 0.04
    area_min_ratio     = 0.001
    max_angle_cos      = 0.3
    gaussian_blur_size = 5

    while True:
        clock.tick()
        img = sensor.snapshot()
        img_np = img.to_numpy_ref()

        rects = cv_lite.grayscale_find_rectangles_with_corners(
            [cam_h, cam_w], img_np,
            canny_thresh1, canny_thresh2,
            approx_epsilon,
            area_min_ratio,
            max_angle_cos,
            gaussian_blur_size
        )

        # 1. 预设默认目标为屏幕中心 (作为没找到目标或目标太小的情况)
        target_x = screen_center_x
        target_y = screen_center_y

        if rects:
            # 2. 找面积最大的矩形
            r = max(rects, key=lambda r: r[2] * r[3])
            area = r[2] * r[3]

            # 3. 增加判断条件：只有面积达标，才将其视为有效目标
            if area >= 6000:
                # 画矩形框和四个角点
                img.draw_rectangle(r[0], r[1], r[2], r[3], color=(255, 255, 255), thickness=2)
                img.draw_cross(r[4],  r[5],  color=(255, 255, 255), size=5, thickness=2)
                img.draw_cross(r[6],  r[7],  color=(255, 255, 255), size=5, thickness=2)
                img.draw_cross(r[8],  r[9],  color=(255, 255, 255), size=5, thickness=2)
                img.draw_cross(r[10], r[11], color=(255, 255, 255), size=5, thickness=2)

                # 更新目标坐标为有效矩形的对角线交点
                target_x = (r[4] + r[8]) // 2
                target_y = (r[5] + r[9]) // 2

        # 4. 统一绘制最终的目标十字准星
        # (如果面积>=6000，这里画的就是追踪目标的中心；否则就是屏幕中心)
        img.draw_cross(target_x, target_y, color=(0, 0, 0), size=10, thickness=2)

        # 计算原始的带符号误差 (目标相对于屏幕中心的位置)
        raw_err_x = target_x - screen_center_x
        # 设置死区，避免微小抖动
        if abs(raw_err_x) < 3:
            dx = 0
        else:
            #应用一阶低通滤波
            filtered_err_x = (ALPHA_NUM * raw_err_x + (ALPHA_DEN - ALPHA_NUM) * filtered_err_x) // ALPHA_DEN
            # 将滤波后的误差转换为电机需要的方向 (dir) 和大小 (dx/dy)
            if filtered_err_x >= 0:
                dir_x = 0
                dx = filtered_err_x
            else:
                dir_x = 1
                dx = -filtered_err_x
            dx = dx * 20

        if uart2:
            try:
                send_order(1, dir_x, int(dx))
                #uart2.write(bytes(synx_cmd))
#                while not uart2.read(): # 等待电机返回确认信号
#                    pass
            except Exception as e:
                print(f"串口发送错误: {e}")

        # 绘制中心
        img.draw_circle(screen_center_x, screen_center_y, 4, color=(0, 0, 0), thickness=2, fill=False)
        img.draw_string_advanced(0, 0, 30, f"FPS:{clock.fps():.3f}" , color=(0, 0, 0))
        if rects:
            img.draw_string_advanced(0, 40, 30, f"area:{area}" , color=(0, 0, 0))

        Display.show_image(img, x=display_x, y=display_y)

        raw_err_y = target_y - screen_center_y

        if abs(raw_err_y) < 3:

            dy = 0
        else:
            filtered_err_y = (ALPHA_NUM * raw_err_y + (ALPHA_DEN - ALPHA_NUM) * filtered_err_y) // ALPHA_DEN
            if filtered_err_y >= 0:
                dir_y = 1
                dy = filtered_err_y
            else:
                dir_y = 0
                dy = -filtered_err_y
            dy = dy * 7

        if uart2:
            try:
                send_order(2, dir_y, int(dy))
                #uart2.write(bytes(synx_cmd))
                #while not uart2.read(): # 等待电机返回确认信号
                    #pass
            except Exception as e:
                print(f"串口发送错误: {e}")

        gc.collect()

except KeyboardInterrupt:
    pass
except BaseException as e:
    sys.print_exception(e)
finally:
    if isinstance(sensor, Sensor):
        sensor.stop()
    Display.deinit()
    os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
    time.sleep_ms(100)
    MediaManager.deinit()
