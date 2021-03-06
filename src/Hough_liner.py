#!/usr/bin/env python

from pickle import FALSE
import rospy, random, cv2, math
import numpy as np
from collections import deque
from yolov3_trt_ros.msg import BoundingBox, BoundingBoxes

from liner import Liner


class HoughLiner(Liner):

    fps = 30.0    
    target_b = 128
    start = False

    prev_angles = deque([0])
    q_len = 15
    straight_thres = 10
    pos_differ_thres = 550
    
    turn_signal= None
    ready2turn = False
    force_turn_count = 0
    force_go_count = 0
    stop_count = 0
    ignore_count = 0

    font = cv2.FONT_HERSHEY_SIMPLEX

    def __init__(self, node_name):
        Liner.__init__(self, node_name)
        self.sub_itrpt = rospy.Subscriber('/yolov3_trt_ros/detections', BoundingBoxes, self.callback_itrpt, queue_size=1)

    def callback(self, msg):
        if self.start == False:
            return

        if self.force_go_count > 0:
            print("force go")
            self.force_go_count -= 1
            self.controller.go(0)
            return
        if self.stop_count > 0:
            print("stop_count:", self.stop_count)
            self.stop_count -= 1
            self.controller.stop()
            return
        elif self.ignore_count > 0:
            self.ignore_count -= 1

        if self.force_turn_count > 0:
            print("force_turn")
            self.force_turn_count -= 1
            if self.turn_signal == 0:
                self.controller.go(-35)
            else:
                self.controller.go(40)
            return

        frame = self.imgmsg2numpy(msg)
        self.width_offset = 0
        self.width = msg.width
        self.height = msg.height
        self.offset = 310
        self.gap = 60
        self.lpos = self.width_offset
        self.rpos = self.width - self.width_offset

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (0, 0), 2.0)

        low_thres = 60
        high_thres = 70

        roi = gray[self.offset:self.offset + self.gap, 0 + self.width_offset:self.width - self.width_offset]
        curr_b = np.mean(roi)

        gray = gray + np.uint8(self.target_b - curr_b)

        ret, gray = cv2.threshold(gray, 145, 255, cv2.THRESH_BINARY_INV)

        #cv2.imshow('gray', gray)
        
        edge = cv2.Canny(np.uint8(gray), low_thres, high_thres)
        roi = edge[self.offset:self.offset + self.gap, 0 + self.width_offset:self.width - self.width_offset]

        lines = cv2.HoughLinesP(roi, 1, math.pi / 180, 30, 30, 10)

        if lines is not None:
            left_lines, right_lines, mid = self.divide_left_right(lines)
            frame, self.lpos = self.get_line_pos(frame, left_lines, left=True)
            frame, self.rpos = self.get_line_pos(frame, right_lines, right=True)


            frame = self.draw_lines(frame, left_lines)
            frame = self.draw_lines(frame, right_lines)
            frame = self.draw_rectangle(frame)

            # if lpos not detected
            if self.lpos == self.width_offset:
                if self.rpos > self.width * 0.6:
                    angle = 0
                else:
                    angle = -40
            # if rpos not detected
            elif self.rpos == self.width - self.width_offset:
                if self.lpos < self.width * 0.4:
                    angle = 0
                else:
                    angle = 40
            # if both pos not detected
            else:
                center = (self.lpos + self.rpos) / 2
                error = (center - self.width / 2)
                if abs(self.lpos - self.rpos) < 135 or self.rpos < self.lpos:
                    if self.prev_angles[-1] > 0:
                        angle = 40
                    elif self.prev_angles[-1] < 0:
                        angle = -40
                    else:
                        angle = 0
           
                else:
                    angle = self.pid.pid_control(error) * 1
            
            # if noise occur while on the curve, keep direction (filltering)
            if (self.turn_signal == 0 and angle > self.straight_thres) or (self.turn_signal == 1 and angle < -self.straight_thres):
                angle = 0
                print("force_turn")
                self.force_turn()

            # pop oldest prev_angle
            if len(self.prev_angles) >= self.q_len:
                self.prev_angles.popleft()

            # push current angle
            self.prev_angles.append(angle)

            # If xycar plan to turn left or right soon
            if self.ready2turn:
                if self.lpos != self.width_offset and self.rpos != self.width - self.width_offset and self.rpos - self.lpos > self.pos_differ_thres:
                    print("force_turn")
                    self.force_turn()
            else:
                # check straight
                avg_angle = abs(sum(self.prev_angles)/self.q_len)
                if avg_angle < self.straight_thres:
                    #print("straight_________________________")
                    self.turn_signal = None

            # print("lpos: {}, rpos: {}".format(self.lpos, self.rpos))
        else:
            if self.turn_signal == 0:
                angle = -50
            elif self.turn_signal == 1:
                angle = 50
            else:
                angle = 0

        # steering
        self.controller.go(angle)


        # for Debug

        # cv2.putText(frame, "angle " + str(angle), (50, 100), font, 1, (255, 0, 0), 2)
        # cv2.putText(frame, str(self.lpos) + ", " + str(self.rpos), (50, 440), font, 1, (255, 0, 0), 2)
        cv2.putText(frame, "ready to turn" + str(self.ready2turn), (440, 50), self.font, 1, (255, 0, 0), 2)
        cv2.imshow('frame', frame)

    def callback_itrpt(self, msg):
        if self.start == False:
            self.start = True
        if self.ignore_count > 0 or len(msg.bounding_boxes) == 0:
            return

        max_size = 0
        max_class = -1

        for bounding_box in msg.bounding_boxes:
            class_id = bounding_box.id
            xmin, ymin, xmax, ymax = bounding_box.xmin, bounding_box.ymin, bounding_box.xmax, bounding_box.ymax
            size = self.get_size(xmin, ymin, xmax, ymax)
            if size > max_size:
                max_size = size
                max_class = class_id

        if (max_class == 0 or max_class == 1) and size < 3200:
            return
        elif max_class in [5, 6, 7] and size < 7000:
            return
        elif max_class in [2, 3, 4] and size < 4000:
            return

        print("class_id", max_class)

        if max_class == 0:
            if self.turn_signal is not None:
                return
            self.turn_signal = 0
            self.ready2turn = True
        elif max_class == 1:
            if self.turn_signal is not None:
                return
            self.turn_signal = 1
            self.ready2turn = True
        # stop immediately 5sec when stop, crosswalk, uturn sign detected 
        elif max_class == 2 or max_class == 3:
            self.stop_5sec()
        elif max_class >= 5:
            #self.force_turn_count = 0
            if max_class == 5:
                self.stop()
                self.turn_signal = None
                print("red")
            elif max_class == 6:
                print("yello")
            elif max_class == 7:
                if self.turn_signal == None:
                    self.go_now()
                    self.force_go()
                print("green")


    def draw_lines(self, img, lines):
        for line in lines:
            x1, y1, x2, y2 = line
            color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            img = cv2.line(img, (x1 + self.width_offset, y1 + self.offset), (x2 + self.width_offset, y2 + self.offset),
                           color, 2)
        return img

    def draw_rectangle(self, img):
        ccen = (self.lpos + self.rpos) / 2
        ocen = self.width / 2

        cv2.rectangle(img, (self.lpos - 5, 15 + self.offset), (self.lpos + 5, 25 + self.offset), (0, 255, 0), 2)
        cv2.rectangle(img, (self.rpos - 5, 15 + self.offset), (self.rpos + 5, 25 + self.offset), (0, 255, 0), 2)
        cv2.rectangle(img, (ccen - 5, 15 + self.offset), (ccen + 5, 25 + self.offset), (0, 255, 0), 2)
        cv2.rectangle(img, (ocen - 5, 15 + self.offset), (ocen + 5, 25 + self.offset), (0, 0, 255), 2)

        return img

    def divide_left_right(self, lines):
        low_grad_thres = 0
        high_grad_thres = 5

        filtered_lines = []
        left_lines = []
        right_lines = []

        max_grad = -20
        min_grad = 20
        max_x = 0
        min_x = self.width

        for line in lines:
            x1, y1, x2, y2 = line[0]
            if y2 - y1 == 0:
                grad = 0
            else:
                grad = float(x2 - x1) / float(y2 - y1)

            if (abs(grad) > low_grad_thres) and (abs(grad) < high_grad_thres):
                if max_grad < grad:
                    max_grad = grad
                if min_grad > grad:
                    min_grad = grad
                if x1 > max_x:
                    max_x = x1
                if x1 < min_x:
                    min_x = x1

                filtered_lines.append((line, grad))

        if max_x - min_x > 400:
            mid = (max_grad + min_grad) / 2
        else:
            mid = 0

        for line, grad in filtered_lines:
            x1, y1, x2, y2 = line[0]
            if grad < mid:
                left_lines.append(line[0].tolist())
            else:
                right_lines.append(line[0].tolist())

        return left_lines, right_lines, mid

    def get_line_params(self, lines):
        x_sum = 0.0
        y_sum = 0.0
        m_sum = 0.0

        size = len(lines)
        if not size:
            return 0, 0

        for line in lines:
            x1, y1, x2, y2 = line

            m = float(y2 - y1) / float(x2 - x1)

            x_sum += x1 + x2
            y_sum += y1 + y2
            m_sum += m

        x_mean = x_sum / (size * 2)
        y_mean = y_sum / (size * 2)
        m = m_sum / size
        b = y_mean - m * x_mean

        return m, b

    def get_line_pos(self, img, lines, left=False, right=False):
        m, b = self.get_line_params(lines)

        if m == 0 and b == 0:
            if left:
                pos = 0
            if right:
                pos = self.width
        else:
            y = self.gap / 2
            pos = int((y - b) / m)

            b += self.offset
            x1 = (self.height - b) / float(m)
            x2 = ((self.height / 2) - b) / float(m)

            cv2.line(img, (int(x1) + self.width_offset, self.height), (int(x2) + self.width_offset, self.height / 2),
                     (255, 0, 0), 3)

        return img, pos + self.width_offset

    def force_turn(self):
        self.force_turn_count = 60
        self.ready2turn = False

        if self.turn_signal == 0:
            pass
        elif self.turn_signal == 1:
            pass
        else:
            raise Exception("self.turn_signal cannot be None")

    def force_go(self):
        self.force_go_count = 40

    def stop_5sec(self):
        self.stop_count = 7*self.fps
        # ignore msg from detector
        self.ignore_count = 7*self.fps

    def stop(self):
        self.stop_count = 7*self.fps

    def go_now(self):
        self.stop_count = 0
        #self.ignore_count = 0

    def get_size(self, xmin, ymin, xmax, ymax):
        return (xmax-xmin)*(ymax-ymin)
