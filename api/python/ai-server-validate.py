import cv2
from flask import Flask, request, jsonify, send_file
import time
from flask_cors import CORS
import numpy as np
import bchlib
from PIL import Image,ImageOps
import numpy as np
import tensorflow as tf
from tensorflow.python.saved_model import tag_constants
from tensorflow.python.saved_model import signature_constants
from io import BytesIO

app = Flask(__name__)
CORS(app)

def get_intersect(p1, p2, p3, p4):
    s = np.vstack([p1,p2,p3,p4])
    h = np.hstack((s, np.ones((4, 1))))
    l1 = np.cross(h[0], h[1])
    l2 = np.cross(h[2], h[3])
    x, y, z = np.cross(l1, l2)
    if z == 0:
        return (0,0)
    return (x/z, y/z)

def poly_area(poly):
    return 0.5*np.abs(np.dot(poly[:,0],np.roll(poly[:,1],1))-np.dot(poly[:,1],np.roll(poly[:,0],1)))

def order_points(pts):
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]

    diff = np.diff(pts, axis = 1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect

config = tf.compat.v1.ConfigProto()
config.gpu_options.allow_growth = True
detector_graph = tf.compat.v1.Graph()
decoder_graph = tf.compat.v1.Graph()

with detector_graph.as_default():
    detector_sess = tf.compat.v1.Session()
    detector_model = tf.compat.v1.saved_model.loader.load(detector_sess, [tag_constants.SERVING], 'detector_models/stegastamp_detector')

    detector_input_name = detector_model.signature_def[signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY].inputs['image'].name
    detector_input = detector_graph.get_tensor_by_name(detector_input_name)

    detector_output_name = detector_model.signature_def[signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY].outputs['detections'].name
    detector_output = detector_graph.get_tensor_by_name(detector_output_name)

with decoder_graph.as_default():
    decoder_sess = tf.compat.v1.Session()
    decoder_model = tf.compat.v1.saved_model.loader.load(decoder_sess, [tag_constants.SERVING], 'saved_models/stegastamp_pretrained')

    decoder_input_name = decoder_model.signature_def[signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY].inputs['image'].name
    decoder_input = decoder_graph.get_tensor_by_name(decoder_input_name)

    decoder_output_name = decoder_model.signature_def[signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY].outputs['decoded'].name
    decoder_output = decoder_graph.get_tensor_by_name(decoder_output_name)

BCH_POLYNOMIAL = 137 #137
BCH_BITS = 5 #5
bch = bchlib.BCH(prim_poly=BCH_POLYNOMIAL, t=BCH_BITS)

# handle error
@app.errorhandler(400)
def not_found_error(error):
    return jsonify({'error': 'Bad request'}), 400

@app.route('/validate_image', methods=['POST'])
async def validate_image():

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if file:

        image_start = time.time()

        np_array = np.frombuffer(file.read(), np.uint8)
        frame = cv2.imdecode(np_array, cv2.IMREAD_COLOR)

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (9,9), 0)
        thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]

        # Morph close
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5,5))
        close = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=2)

        # Find contours and filter for QR code
        cnts = cv2.findContours(close, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cnts = cnts[0] if len(cnts) == 2 else cnts[1]

        image_end = time.time()
        with open("Times.txt","a") as f:
            f.write(f"Image Time : {image_end-image_start}s\n")

        for c in cnts:
            peri = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.04 * peri, True)
            x,y,w,h = cv2.boundingRect(approx)

            area = cv2.contourArea(c)
            ar = w / float(h)
            if len(approx) == 4 and area > 1000 and (ar > .85 and ar < 1.3):
                points = np.array([[x,y],[x+w,y],[x,y+h],[x+w,y+h]])
                corners_full_res = order_points(points)
                pts_dst = np.array([[0,0],[399,0],[399,399],[0,399]])
                h, status = cv2.findHomography(corners_full_res, pts_dst)
                try:
                    warped_im = cv2.warpPerspective(frame_rgb, h, (400,400))
                    cv2.imwrite("out/Warped.png",warped_im)

                    w_im = warped_im.astype(np.float32)
                    w_im /= 255.
                except:
                    continue

                for im_rotation in range(4):
                    w_rotated = np.rot90(w_im, im_rotation)
                    recovered_secret = decoder_sess.run([decoder_output],feed_dict={decoder_input:[w_rotated]})[0][0]
                    recovered_secret = list(recovered_secret)
                    recovered_secret = [int(i) for i in recovered_secret]

                    packet_binary = "".join([str(bit) for bit in recovered_secret[:96]])
                    footer = recovered_secret[96:]
                    if np.sum(footer) > 0:
                        continue
                    packet = bytes(int(packet_binary[i : i + 8], 2) for i in range(0, len(packet_binary), 8))
                    packet = bytearray(packet)

                    data, ecc = packet[:-bch.ecc_bytes], packet[-bch.ecc_bytes:]

                    bitflips = bch.decode(data, ecc)

                    if bitflips != -1:
                        try:
                            code = data.decode("utf-8")
                            return jsonify({"success" : True, "code" : str(code)})
                        except:
                            continue



    return jsonify({"success" : False, "message" : "No Code Detected"})


if __name__ == '__main__':
    app.run(debug=True, port=8080)