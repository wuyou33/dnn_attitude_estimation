#!/usr/bin/env python3

import rospy
from sensor_msgs.msg import Image as ImageMsg
from geometry_msgs.msg import Vector3Stamped
from sensor_msgs.msg import Imu

from cv_bridge import CvBridge, CvBridgeError

import cv2
from PIL import Image
import math

import torch
from torchvision import models
import torch.nn as nn
from torchvision import transforms

import mle_network

class AttitudeEstimation:
    def __init__(self, net):
        print("--- mle_prediction ---")
        ## parameter-msg
        self.frame_id = rospy.get_param("/frame_id", "/base_link")
        print("frame_id = ", frame_id)
        self.num_cameras = rospy.get_param("/num_cameras", 1)
        print("self.num_cameras = ", self.num_cameras)
        ## parameter-dnn
        weights_path = rospy.get_param("/weights_path", "../../weights/mle.pth")
        print("weights_path = ", weights_path)
        resize = rospy.get_param("/resize", 224)
        print("resize = ", resize)
        mean_element = rospy.get_param("/mean_element", 0.5)
        print("mean_element = ", mean_element)
        std_element = rospy.get_param("/std_element", 0.5)
        print("std_element = ", std_element)
        ## subscriber
        self.list_sub = []
        for camera_idx in range(self.num_cameras):
            sub_imgae = rospy.Subscriber("/image_raw" + str(camera_idx), ImageMsg, self.callbackImage, callback_args=camera_idx, queue_size=1, buff_size=2**24)
            self.list_sub.append(sub_image)
        ## publisher
        self.pub_vector = rospy.Publisher("/dnn/g_vector", Vector3Stamped, queue_size=1)
        self.pub_accel = rospy.Publisher("/dnn/g_vector_with_cov", Imu, queue_size=1)
        ## msg
        self.v_msg = Vector3Stamped()
        self.accel_msg = Imu()
        ## cv_bridge
        self.bridge = CvBridge()
        self.list_img_cv = [np.empty(0)]*self.num_cameras
        ## flag
        self.done_init = False
        self.list_got_new_img = [False]*self.num_cameras
        ## dnn
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        print("device = ", device)
        self.img_transform = self.getImageTransform(resize, mean_element, std_element)
        self.net = getNetwork(resize, weights_path)
        ## done
        self.done_init = True

    def getImageTransform(self, resize, mean_element, std_element):
        mean = ([mean_element, mean_element, mean_element])
        std = ([std_element, std_element, std_element])
        img_transform = transforms.Compose([
            transforms.Resize(resize),
            transforms.CenterCrop(resize),
            transforms.ToTensor(),
            transforms.Normalize(mean, std)
        ])
        return img_transform

    def getNetwork(self, resize, weights_path):
        net = network.OriginalNet(self.num_cameras, resize=resize, use_pretrained=False)
        print(net)
        net.to(self.device)
        net.eval()
        ## load
        if torch.cuda.is_available():
            loaded_weights = torch.load(weights_path)
            print("Loaded [GPU -> GPU]: ", weights_path)
        else:
            loaded_weights = torch.load(weights_path, map_location={"cuda:0": "cpu"})
            print("Loaded [GPU -> CPU]: ", weights_path)
        net.load_state_dict(loaded_weights)
        return net

    def callbackImage(self, msg, camera_idx):
        if self.done_init:
            print("----------")
            start_clock = rospy.get_time()
            try:
                img_cv = self.bridge.imgmsg_to_cv2(msg, "bgr8")
                print("img_cv.shape = ", img_cv.shape)
                self.list_img_cv[camera_idx] = img_cv
                self.list_got_new_img = True
                if all(list_got_new_img):
                    outputs = self.dnnPrediction(img_cv)
                    self.inputToMsg(outputs)
                    self.publication(msg.header.stamp)
                    print("Period [s]: ", rospy.get_time() - start_clock, "Frequency [hz]: ", 1/(rospy.get_time() - start_clock))
            except CvBridgeError as e:
                print(e)

    def dnnPrediction(self):
        ## inference
        inputs = transformImage()
        print("input.size = ", inputs.size)
        outputs = self.net(inputs)
        print("outputs = ", outputs)
        ## reset
        self.list_got_new_img = [False]*self.num_cameras
        print("self.list_got_new_img = ", self.list_got_new_img)
        return outputs

    def transformImage(self):
        for i in range(self.num_cameras):
            img_pil = self.cvToPIL(list_img_cv[i])
            img_tensor = self.img_transform(img_pil)
            if i == 0:
                combined_img_tensor = img_tensor
            else:
                # combined_img_tensor = torch.cat((combined_img_tensor, img_tensor), dim=2)
                combined_img_tensor = torch.cat((img_tensor, combined_img_tensor), dim=2)
        inputs = combined_img_tensor.unsqueeze_(0)
        inputs = inputs.to(self.device)
        return inputs

    def cvToPIL(self, img_cv):
        img_cv = cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGB)
        img_pil = Image.fromarray(img_cv)
        return img_pil

    def inputToMsg(self, outputs):
        ## get covariance matrix
        cov = self.getCovMatrix(outputs)
        ## tensor to numpy
        outputs = outputs[0].cpu().detach().numpy()
        cov = cov[0].cpu().detach().numpy()
        ## Vector3Stamped
        self.v_msg.vector.x = -outputs[0]
        self.v_msg.vector.y = -outputs[1]
        self.v_msg.vector.z = -outputs[2]
        ## Imu
        self.inputNanToImuMsg(self.accel_msg)
        self.accel_msg.linear_acceleration.x = -outputs[0]
        self.accel_msg.linear_acceleration.y = -outputs[1]
        self.accel_msg.linear_acceleration.z = -outputs[2]
        for i in range(cov.size):
            self.accel_msg.linear_acceleration_covariance[i] = cov[i//3, i%3]
        ## print
        print("cov = ", cov)

    def getCovMatrix(self, outputs):
        L = self.getTriangularMatrix(outputs)
        Ltrans = torch.transpose(L, 1, 2)
        LL = torch.bmm(L, Ltrans)
        return LL

    def getTriangularMatrix(self, outputs):
        elements = outputs[:, 3:9]
        L = torch.zeros(outputs.size(0), elements.size(1)//2, elements.size(1)//2)
        L[:, 0, 0] = torch.exp(elements[:, 0])
        L[:, 1, 0] = elements[:, 1]
        L[:, 1, 1] = torch.exp(elements[:, 2])
        L[:, 2, 0] = elements[:, 3]
        L[:, 2, 1] = elements[:, 4]
        L[:, 2, 2] = torch.exp(elements[:, 5])
        return L

    def inputNanToImuMsg(self, imu):
        imu.orientation.x = math.nan
        imu.orientation.y = math.nan
        imu.orientation.z = math.nan
        imu.orientation.w = math.nan
        imu.angular_velocity.x = math.nan
        imu.angular_velocity.y = math.nan
        imu.angular_velocity.z = math.nan
        imu.linear_acceleration.x = math.nan
        imu.linear_acceleration.y = math.nan
        imu.linear_acceleration.z = math.nan
        for i in range(len(imu.linear_acceleration_covariance)):
            imu.orientation_covariance[i] = math.nan
            imu.angular_velocity_covariance[i] = math.nan
            imu.linear_acceleration_covariance[i] = math.nan

    def publication(self, stamp):
        print("delay[s]: ", (rospy.Time.now() - stamp).to_sec())
        ## Vector3Stamped
        self.v_msg.header.stamp = stamp
        self.v_msg.header.frame_id = self.frame_id
        self.pub_vector.publish(self.v_msg)
        ## Imu
        self.accel_msg.header.stamp = stamp
        self.accel_msg.header.frame_id = self.frame_id
        self.pub_accel.publish(self.accel_msg)

def main():
    ## Node
    rospy.init_node('attitude_estimation', anonymous=True)
    ## Network
    net = mle_network.OriginalNet()
    print(net)
    net.to(device)
    ## Load weights
    weights_was_saved_in_same_device = True
    if weights_was_saved_in_same_device:
        ## saved in CPU -> load in CPU, saved in GPU -> load in GPU
        print("Loaded: GPU -> GPU or CPU -> CPU")
        load_weights = torch.load(weights_path)
    else:
        ## saved in GPU -> load in CPU
        print("Loaded: GPU -> CPU")
        load_weights = torch.load(weights_path, map_location={"cuda:0": "cpu"})
    net.load_state_dict(load_weights)
    ## set as eval
    net.eval()

    attitude_estimation = AttitudeEstimation(frame_id, device, size, mean, std, net)

    rospy.spin()

if __name__ == '__main__':
    main()
