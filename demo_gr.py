import copy
import json
import os
import os.path as osp
import queue
import secrets
import threading
import time
import zipfile
import shutil
from datetime import datetime
from glob import glob
from pathlib import Path
from typing import Literal
import subprocess

import gradio as gr
import httpx
import imageio.v3 as iio
import numpy as np
import torch
import torch.nn.functional as F
import tyro
import viser
import viser.transforms as vt
from einops import rearrange
from gradio import networking
from gradio.context import LocalContext
from gradio.tunneling import CERTIFICATE_PATH, Tunnel

from seva.eval import (
    IS_TORCH_NIGHTLY,
    chunk_input_and_test,
    create_transforms_simple,
    infer_prior_stats,
    run_one_scene,
    transform_img_and_K,
)
from seva.geometry import (
    DEFAULT_FOV_RAD,
    get_default_intrinsics,
    get_preset_pose_fov,
    normalize_scene,
)
from seva.gui import define_gui
from seva.model import SGMWrapper
from seva.modules.autoencoder import AutoEncoder
from seva.modules.conditioner import CLIPConditioner
from seva.modules.preprocessor import Dust3rPipeline
from seva.sampling import DDPMDiscretization, DiscreteDenoiser
from seva.utils import load_model

device = "cuda:0"


# Constants.
WORK_DIR = "work_dirs/demo_gr"
MAX_SESSIONS = 1
ADVANCE_EXAMPLE_MAP = [
    (
        "assets/advance/blue-car.jpg",
        ["assets/advance/blue-car.jpg"],
    ),
    (
        "assets/advance/garden-4_0.jpg",
        [
            "assets/advance/garden-4_0.jpg",
            "assets/advance/garden-4_1.jpg",
            "assets/advance/garden-4_2.jpg",
            "assets/advance/garden-4_3.jpg",
        ],
    ),
    (
        "assets/advance/vgg-lab-4_0.png",
        [
            "assets/advance/vgg-lab-4_0.png",
            "assets/advance/vgg-lab-4_1.png",
            "assets/advance/vgg-lab-4_2.png",
            "assets/advance/vgg-lab-4_3.png",
        ],
    ),
    (
        "assets/advance/telebooth-2_0.jpg",
        [
            "assets/advance/telebooth-2_0.jpg",
            "assets/advance/telebooth-2_1.jpg",
        ],
    ),
    (
        "assets/advance/backyard-7_0.jpg",
        [
            "assets/advance/backyard-7_0.jpg",
            "assets/advance/backyard-7_1.jpg",
            "assets/advance/backyard-7_2.jpg",
            "assets/advance/backyard-7_3.jpg",
            "assets/advance/backyard-7_4.jpg",
            "assets/advance/backyard-7_5.jpg",
            "assets/advance/backyard-7_6.jpg",
        ],
    ),
]

if IS_TORCH_NIGHTLY:
    COMPILE = True
    os.environ["TORCHINDUCTOR_AUTOGRAD_CACHE"] = "1"
    os.environ["TORCHINDUCTOR_FX_GRAPH_CACHE"] = "1"
else:
    COMPILE = False

# Shared global variables across sessions.
DUST3R = Dust3rPipeline(device=device)  # type: ignore
MODEL = SGMWrapper(load_model(device="cpu", verbose=True).eval()).to(device)
AE = AutoEncoder(chunk_size=1).to(device)
CONDITIONER = CLIPConditioner().to(device)
DISCRETIZATION = DDPMDiscretization()
DENOISER = DiscreteDenoiser(discretization=DISCRETIZATION, num_idx=1000, device=device)
VERSION_DICT = {
    "H": 576,
    "W": 576,
    "T": 21,
    "C": 4,
    "f": 8,
    "options": {},
}
SERVERS = {}
ABORT_EVENTS = {}

if COMPILE:
    MODEL = torch.compile(MODEL)
    CONDITIONER = torch.compile(CONDITIONER)
    AE = torch.compile(AE)


class SevaRenderer(object):
    def __init__(self, server: viser.ViserServer):
        self.server = server
        self.gui_state = None
        self._current_export_dir = None

    def create_download_zip(self, output_dir: str) -> str:
        """Create a zip file containing all output data and return its path."""
        zip_path = output_dir + ".zip"
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(output_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, output_dir)
                    zipf.write(file_path, arcname)
        return zip_path

    def preprocess(
        self, input_img_path_or_tuples: list[tuple[str, None]] | str
    ) -> tuple[dict, dict, dict]:
        # Simply hardcode these such that aspect ratio is always kept and
        # shorter side is resized to 576. This is only to make GUI option fewer
        # though, changing it still works.
        shorter: int = 576
        # Has to be 64 multiple for the network.
        shorter = round(shorter / 64) * 64

        if isinstance(input_img_path_or_tuples, str):
            # Assume `Basic` demo mode: just hardcode the camera parameters and ignore points.
            input_imgs = torch.as_tensor(
                iio.imread(input_img_path_or_tuples) / 255.0, dtype=torch.float32
            )[None, ..., :3]
            input_imgs = transform_img_and_K(
                input_imgs.permute(0, 3, 1, 2),
                shorter,
                K=None,
                size_stride=64,
            )[0].permute(0, 2, 3, 1)
            input_Ks = get_default_intrinsics(
                aspect_ratio=input_imgs.shape[2] / input_imgs.shape[1]
            )
            input_c2ws = torch.eye(4)[None]
            # Simulate a small time interval such that gradio can update
            # propgress properly.
            time.sleep(0.1)
            return (
                {
                    "input_imgs": input_imgs,
                    "input_Ks": input_Ks,
                    "input_c2ws": input_c2ws,
                    "input_wh": (input_imgs.shape[2], input_imgs.shape[1]),
                    "points": [np.zeros((0, 3))],
                    "point_colors": [np.zeros((0, 3))],
                    "scene_scale": 1.0,
                },
                gr.update(visible=False),
                gr.update(),
            )
        else:
            # Assume `Advance` demo mode: use dust3r to extract camera parameters and points.
            img_paths = [p for (p, _) in input_img_path_or_tuples]
            (
                input_imgs,
                input_Ks,
                input_c2ws,
                points,
                point_colors,
            ) = DUST3R.infer_cameras_and_points(img_paths)
            num_inputs = len(img_paths)
            if num_inputs == 1:
                input_imgs, input_Ks, input_c2ws, points, point_colors = (
                    input_imgs[:1],
                    input_Ks[:1],
                    input_c2ws[:1],
                    points[:1],
                    point_colors[:1],
                )
            input_imgs = [img[..., :3] for img in input_imgs]
            # Normalize the scene.
            point_chunks = [p.shape[0] for p in points]
            point_indices = np.cumsum(point_chunks)[:-1]
            input_c2ws, points, _ = normalize_scene(  # type: ignore
                input_c2ws,
                np.concatenate(points, 0),
                camera_center_method="poses",
            )
            points = np.split(points, point_indices, 0)
            # Scale camera and points for viewport visualization.
            scene_scale = np.median(
                np.ptp(np.concatenate([input_c2ws[:, :3, 3], *points], 0), -1)
            )
            input_c2ws[:, :3, 3] /= scene_scale
            points = [point / scene_scale for point in points]
            input_imgs = [
                torch.as_tensor(img / 255.0, dtype=torch.float32) for img in input_imgs
            ]
            input_Ks = torch.as_tensor(input_Ks)
            input_c2ws = torch.as_tensor(input_c2ws)
            new_input_imgs, new_input_Ks = [], []
            for img, K in zip(input_imgs, input_Ks):
                img = rearrange(img, "h w c -> 1 c h w")
                # If you don't want to keep aspect ratio and want to always center crop, use this:
                # img, K = transform_img_and_K(img, (shorter, shorter), K=K[None])
                img, K = transform_img_and_K(img, shorter, K=K[None], size_stride=64)
                assert isinstance(K, torch.Tensor)
                K = K / K.new_tensor([img.shape[-1], img.shape[-2], 1])[:, None]
                new_input_imgs.append(img)
                new_input_Ks.append(K)
            input_imgs = torch.cat(new_input_imgs, 0)
            input_imgs = rearrange(input_imgs, "b c h w -> b h w c")[..., :3]
            input_Ks = torch.cat(new_input_Ks, 0)
            return (
                {
                    "input_imgs": input_imgs,
                    "input_Ks": input_Ks,
                    "input_c2ws": input_c2ws,
                    "input_wh": (input_imgs.shape[2], input_imgs.shape[1]),
                    "points": points,
                    "point_colors": point_colors,
                    "scene_scale": scene_scale,
                },
                gr.update(visible=False),
                gr.update()
                if num_inputs <= 10
                else gr.update(choices=["interp"], value="interp"),
            )

    def visualize_scene(self, preprocessed: dict):
        server = self.server
        server.scene.reset()
        server.gui.reset()
        set_bkgd_color(server)

        (
            input_imgs,
            input_Ks,
            input_c2ws,
            input_wh,
            points,
            point_colors,
            scene_scale,
        ) = (
            preprocessed["input_imgs"],
            preprocessed["input_Ks"],
            preprocessed["input_c2ws"],
            preprocessed["input_wh"],
            preprocessed["points"],
            preprocessed["point_colors"],
            preprocessed["scene_scale"],
        )
        W, H = input_wh

        server.scene.set_up_direction(-input_c2ws[..., :3, 1].mean(0).numpy())

        # Use first image as default fov.
        assert input_imgs[0].shape[:2] == (H, W)
        if H > W:
            init_fov = 2 * np.arctan(1 / (2 * input_Ks[0, 0, 0].item()))
        else:
            init_fov = 2 * np.arctan(1 / (2 * input_Ks[0, 1, 1].item()))
        init_fov_deg = float(init_fov / np.pi * 180.0)

        frustum_nodes, pcd_nodes = [], []
        for i in range(len(input_imgs)):
            K = input_Ks[i]
            frustum = server.scene.add_camera_frustum(
                f"/scene_assets/cameras/{i}",
                fov=2 * np.arctan(1 / (2 * K[1, 1].item())),
                aspect=W / H,
                scale=0.1 * scene_scale,
                image=(input_imgs[i].numpy() * 255.0).astype(np.uint8),
                wxyz=vt.SO3.from_matrix(input_c2ws[i, :3, :3].numpy()).wxyz,
                position=input_c2ws[i, :3, 3].numpy(),
            )

            def get_handler(frustum):
                def handler(event: viser.GuiEvent) -> None:
                    assert event.client_id is not None
                    client = server.get_clients()[event.client_id]
                    with client.atomic():
                        client.camera.position = frustum.position
                        client.camera.wxyz = frustum.wxyz
                        # Set look_at as the projected origin onto the
                        # frustum's forward direction.
                        look_direction = vt.SO3(frustum.wxyz).as_matrix()[:, 2]
                        position_origin = -frustum.position
                        client.camera.look_at = (
                            frustum.position
                            + np.dot(look_direction, position_origin)
                            / np.linalg.norm(position_origin)
                            * look_direction
                        )

                return handler

            frustum.on_click(get_handler(frustum))  # type: ignore
            frustum_nodes.append(frustum)

            pcd = server.scene.add_point_cloud(
                f"/scene_assets/points/{i}",
                points[i],
                point_colors[i],
                point_size=0.01 * scene_scale,
                point_shape="circle",
            )
            pcd_nodes.append(pcd)

        with server.gui.add_folder("Scene scale", expand_by_default=False, order=200):
            camera_scale_slider = server.gui.add_slider(
                "Log camera scale", initial_value=0.0, min=-2.0, max=2.0, step=0.1
            )

            @camera_scale_slider.on_update
            def _(_) -> None:
                for i in range(len(frustum_nodes)):
                    frustum_nodes[i].scale = (
                        0.1 * scene_scale * 10**camera_scale_slider.value
                    )

            point_scale_slider = server.gui.add_slider(
                "Log point scale", initial_value=0.0, min=-2.0, max=2.0, step=0.1
            )

            @point_scale_slider.on_update
            def _(_) -> None:
                for i in range(len(pcd_nodes)):
                    pcd_nodes[i].point_size = (
                        0.01 * scene_scale * 10**point_scale_slider.value
                    )

        self.gui_state = define_gui(
            server,
            init_fov=init_fov_deg,
            img_wh=input_wh,
            scene_scale=scene_scale,
        )

    def get_target_c2ws_and_Ks_from_gui(self, preprocessed: dict):
        input_wh = preprocessed["input_wh"]
        W, H = input_wh
        gui_state = self.gui_state
        assert gui_state is not None and gui_state.camera_traj_list is not None
        target_c2ws, target_Ks = [], []
        for item in gui_state.camera_traj_list:
            target_c2ws.append(item["w2c"])
            assert item["img_wh"] == input_wh
            K = np.array(item["K"]).reshape(3, 3) / np.array([W, H, 1])[:, None]
            target_Ks.append(K)
        target_c2ws = torch.as_tensor(
            np.linalg.inv(np.array(target_c2ws).reshape(-1, 4, 4))
        )
        target_Ks = torch.as_tensor(np.array(target_Ks).reshape(-1, 3, 3))
        return target_c2ws, target_Ks

    def get_target_c2ws_and_Ks_from_preset(
        self,
        preprocessed: dict,
        preset_traj: Literal[
            "orbit",
            "spiral",
            "lemniscate",
            "zoom-in",
            "zoom-out",
            "dolly zoom-in",
            "dolly zoom-out",
            "move-forward",
            "move-backward",
            "move-up",
            "move-down",
            "move-left",
            "move-right",
        ],
        num_frames: int,
        zoom_factor: float | None,
    ):
        img_wh = preprocessed["input_wh"]
        start_c2w = preprocessed["input_c2ws"][0]
        start_w2c = torch.linalg.inv(start_c2w)
        look_at = torch.tensor([0, 0, 10])
        start_fov = DEFAULT_FOV_RAD
        target_c2ws, target_fovs = get_preset_pose_fov(
            preset_traj,
            num_frames,
            start_w2c,
            look_at,
            -start_c2w[:3, 1],
            start_fov,
            spiral_radii=[1.0, 1.0, 0.5],
            zoom_factor=zoom_factor,
        )
        target_c2ws = torch.as_tensor(target_c2ws)
        target_fovs = torch.as_tensor(target_fovs)
        target_Ks = get_default_intrinsics(
            target_fovs,  # type: ignore
            aspect_ratio=img_wh[0] / img_wh[1],
        )
        return target_c2ws, target_Ks

    def _save_gaussian_splatting_transforms(self, output_dir, img_paths, img_whs, all_c2ws, all_Ks, num_inputs, num_targets):
        """Save camera transforms in formats used by Gaussian Splatting."""
        # Convert from OpenGL to Blender format
        # Blender: +Z up, +Y forward, +X right (right-handed)
        # OpenGL: +Y up, -Z forward, +X right (right-handed)
        # Rotation to convert: X stays, Y->Z, Z->-Y
        # blender_conversion = np.array([
        #     [1, 0, 0, 0],
        #     [0, 0, 1, 0],
        #     [0, -1, 0, 0],
        #     [0, 0, 0, 1]
        # ])
        
        # Extract camera parameters from first camera
        fx, fy = all_Ks[0][0][0], all_Ks[0][1][1]
        cx, cy = all_Ks[0][0][2], all_Ks[0][1][2]
        wh = img_whs[0]
        w, h = int(wh[0]), int(wh[1])
        fov_x = 2 * np.arctan(w / (2 * fx))
        
        # Create the NeRF-like format with global camera parameters
        transforms_format = {
            "camera_angle_x": float(fov_x),
            "fl_x": float(fx),
            "fl_y": float(fy),
            "cx": float(cx),
            "cy": float(cy),
            "w": w,
            "h": h,
            "frames": []
        }
        
        # Add frames data 
        for i in range(len(all_c2ws)):
            # Apply the conversion to Blender coordinate system
            c2w_blender = all_c2ws[i] # @ blender_conversion
            
            # Format for Gaussian Splatting - minimal frame info
            frame_data = {
                "file_path": f"{i:03d}",  # Just the index without .png extension
                "transform_matrix": c2w_blender.tolist()
            }
            transforms_format["frames"].append(frame_data)
        
        # Write the same data to both train and test
        with open(osp.join(output_dir, "transforms_train.json"), "w") as f:
            json.dump(transforms_format, f, indent=4)
            
        # Write identical copy as test transforms
        with open(osp.join(output_dir, "transforms_test.json"), "w") as f:
            json.dump(transforms_format, f, indent=4)
        
        # Also write a copy as validation for compatibility
        with open(osp.join(output_dir, "transforms_val.json"), "w") as f:
            json.dump(transforms_format, f, indent=4)

    def export_output_data(self, preprocessed: dict, output_dir: str = None) -> str:
        """Export the current data for NeRF/Gaussian Splatting processing."""
        # If we're in the middle of rendering or just finished rendering,
        # use the already created export directory
        if hasattr(self, '_current_export_dir') and self._current_export_dir:
            # Just return the existing ZIP
            gr.Info("Using export data from the current render", duration=3)
            return self.create_download_zip(self._current_export_dir)
        
        # Create a timestamp-based output directory if none is provided
        if output_dir is None or output_dir.strip() == "":
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = osp.join(WORK_DIR, f"export_{timestamp}")
            
        input_imgs, input_Ks, input_c2ws, input_wh = (
            preprocessed["input_imgs"],
            preprocessed["input_Ks"],
            preprocessed["input_c2ws"],
            preprocessed["input_wh"],
        )
        
        # Check if we're in GUI mode (Advanced tab) or Basic tab
        if self.gui_state is not None and self.gui_state.camera_traj_list is not None:
            # Advanced tab - get target cameras from GUI
            target_c2ws, target_Ks = self.get_target_c2ws_and_Ks_from_gui(preprocessed)
        else:
            # Basic tab - just use the input camera
            target_c2ws = input_c2ws.clone()
            target_Ks = input_Ks.clone()

        num_inputs = len(input_imgs)
        num_targets = len(target_c2ws)

        input_imgs = (input_imgs.cpu().numpy() * 255.0).astype(np.uint8)
        input_c2ws = input_c2ws.cpu().numpy()
        input_Ks = input_Ks.cpu().numpy()
        target_c2ws = target_c2ws.cpu().numpy()
        target_Ks = target_Ks.cpu().numpy()
        img_whs = np.array(input_wh)[None].repeat(len(input_imgs) + len(target_Ks), 0)

        os.makedirs(output_dir, exist_ok=True)
        img_paths = []
        for i, img in enumerate(input_imgs):
            iio.imwrite(img_path := osp.join(output_dir, f"{i:03d}.png"), img)
            img_paths.append(img_path)
        for i in range(num_targets):
            iio.imwrite(
                img_path := osp.join(output_dir, f"{i + num_inputs:03d}.png"),
                np.zeros((input_wh[1], input_wh[0], 3), dtype=np.uint8),
            )
            img_paths.append(img_path)

        # Convert from OpenCV to OpenGL camera format.
        all_c2ws = np.concatenate([input_c2ws, target_c2ws])
        all_Ks = np.concatenate([input_Ks, target_Ks])
        all_c2ws = all_c2ws @ np.diag([1, -1, -1, 1])
        create_transforms_simple(output_dir, img_paths, img_whs, all_c2ws, all_Ks)
        split_dict = {
            "train_ids": list(range(num_inputs)),
            "test_ids": list(range(num_inputs, num_inputs + num_targets)),
        }
        with open(
            osp.join(output_dir, f"train_test_split_{num_inputs}.json"), "w"
        ) as f:
            json.dump(split_dict, f, indent=4)
        gr.Info(f"Output data saved to {output_dir}", duration=1)
        return self.create_download_zip(output_dir)
    
    def _save_nerf_transforms(self, output_dir, img_paths, img_whs, all_c2ws, all_Ks):
        """Save camera transforms in a format friendly for NeRF training."""
        # Create transforms.json in the style used by many NeRF implementations
        # Extract focal length and other parameters from first camera
        fx, fy = all_Ks[0][0][0], all_Ks[0][1][1]
        cx, cy = all_Ks[0][0][2], all_Ks[0][1][2]
        wh = img_whs[0]
        w, h = int(wh[0]), int(wh[1])
        fov_x = 2 * np.arctan(w / (2 * fx))
        
        # Create the nerf format with global camera parameters
        nerf_format = {
            "camera_angle_x": float(fov_x),
            "fl_x": float(fx),
            "fl_y": float(fy),
            "cx": float(cx),
            "cy": float(cy),
            "w": w,
            "h": h,
            "frames": []
        }
        
        for i, (c2w, img_path) in enumerate(zip(all_c2ws, img_paths)):
            # Format each frame with just the transform matrix and file path
            frame_data = {
                "file_path": osp.relpath(img_path, output_dir),
                "transform_matrix": c2w.tolist()
            }
            nerf_format["frames"].append(frame_data)
            
        with open(osp.join(output_dir, "transforms.json"), "w") as f:
            json.dump(nerf_format, f, indent=4)


    def render(
        self,
        preprocessed: dict,
        session_hash: str,
        seed: int,
        chunk_strategy: str,
        cfg: float,
        preset_traj: Literal[
            "orbit",
            "spiral",
            "lemniscate",
            "zoom-in",
            "zoom-out",
            "dolly zoom-in",
            "dolly zoom-out",
            "move-forward",
            "move-backward",
            "move-up",
            "move-down",
            "move-left",
            "move-right",
        ]
        | None,
        num_frames: int | None,
        zoom_factor: float | None,
        camera_scale: float,
    ):
        render_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        render_dir = osp.join(WORK_DIR, render_name)

        input_imgs, input_Ks, input_c2ws, (W, H) = (
            preprocessed["input_imgs"],
            preprocessed["input_Ks"],
            preprocessed["input_c2ws"],
            preprocessed["input_wh"],
        )
        num_inputs = len(input_imgs)
        if preset_traj is None:
            target_c2ws, target_Ks = self.get_target_c2ws_and_Ks_from_gui(preprocessed)
        else:
            assert num_frames is not None
            assert num_inputs == 1
            input_c2ws = torch.eye(4)[None].to(dtype=input_c2ws.dtype)
            target_c2ws, target_Ks = self.get_target_c2ws_and_Ks_from_preset(
                preprocessed, preset_traj, num_frames, zoom_factor
            )
        all_c2ws = torch.cat([input_c2ws, target_c2ws], 0)
        all_Ks = (
            torch.cat([input_Ks, target_Ks], 0)
            * input_Ks.new_tensor([W, H, 1])[:, None]
        )
        num_targets = len(target_c2ws)
        input_indices = list(range(num_inputs))
        target_indices = np.arange(num_inputs, num_inputs + num_targets).tolist()
        
        # Create export directory and save camera information immediately
        export_dir = osp.join(WORK_DIR, f"export_{render_name}")
        os.makedirs(export_dir, exist_ok=True)
        
        # Save input images
        input_imgs_np = (input_imgs.cpu().numpy() * 255.0).astype(np.uint8)
        img_paths = []
        
        for i, img in enumerate(input_imgs_np):
            img_path = osp.join(export_dir, f"{i:03d}.png")
            iio.imwrite(img_path, img)
            img_paths.append(img_path)
        
        for i in range(num_targets):
            img_path = osp.join(export_dir, f"{i + num_inputs:03d}.png")
            iio.imwrite(img_path, np.zeros((H, W, 3), dtype=np.uint8))
            img_paths.append(img_path)
            
        # Save camera information in NeRF format
        img_whs = np.array((W, H))[None].repeat(len(input_imgs) + len(target_Ks), 0)
        
        # Convert from OpenCV to OpenGL camera format
        all_c2ws_np = all_c2ws.cpu().numpy()
        all_Ks_np = all_Ks.cpu().numpy()
        all_c2ws_gl = all_c2ws_np @ np.diag([1, -1, -1, 1])
        
        # Save camera transforms
        create_transforms_simple(export_dir, img_paths, img_whs, all_c2ws_gl, all_Ks_np)
        
        # Save in NeRF-friendly format
        self._save_nerf_transforms(export_dir, img_paths, img_whs, all_c2ws_gl, all_Ks_np)
        
        # Save in Gaussian Splatting format
        self._save_gaussian_splatting_transforms(export_dir, img_paths, img_whs, all_c2ws_gl, all_Ks_np, num_inputs, num_targets)
        
        # Save split information
        split_dict = {
            "train_ids": list(range(num_inputs)),
            "test_ids": list(range(num_inputs, num_inputs + num_targets)),
        }
        with open(osp.join(export_dir, f"train_test_split_{num_inputs}.json"), "w") as f:
            json.dump(split_dict, f, indent=4)
            
        # Save metadata for debugging
        metadata = {
            "num_inputs": num_inputs,
            "num_targets": num_targets,
            "image_width": W,
            "image_height": H,
            "preset_traj": preset_traj if preset_traj else "custom",
            "render_dir": render_dir,
            "export_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "scene_scale": float(preprocessed.get("scene_scale", 1.0)),
        }
        with open(osp.join(export_dir, "metadata.json"), "w") as f:
            json.dump(metadata, f, indent=4)

        # Create a zip file of the export directory with current (empty) frames
        zip_path = self.create_download_zip(export_dir)
        
        # Set a flag to update the export when rendering is complete
        self._current_export_dir = export_dir
        
        # Get anchor cameras.
        T = VERSION_DICT["T"]
        version_dict = copy.deepcopy(VERSION_DICT)
        num_anchors = infer_prior_stats(
            T,
            num_inputs,
            num_total_frames=num_targets,
            version_dict=version_dict,
        )
        # infer_prior_stats modifies T in-place.
        T = version_dict["T"]
        assert isinstance(num_anchors, int)
        anchor_indices = np.linspace(
            num_inputs,
            num_inputs + num_targets - 1,
            num_anchors,
        ).tolist()
        anchor_c2ws = all_c2ws[[round(ind) for ind in anchor_indices]]
        anchor_Ks = all_Ks[[round(ind) for ind in anchor_indices]]
        # Create image conditioning.
        all_imgs_np = (
            F.pad(input_imgs, (0, 0, 0, 0, 0, 0, 0, num_targets), value=0.0).numpy()
            * 255.0
        ).astype(np.uint8)
        image_cond = {
            "img": all_imgs_np,
            "input_indices": input_indices,
            "prior_indices": anchor_indices,
        }
        # Create camera conditioning (K is unnormalized).
        camera_cond = {
            "c2w": all_c2ws,
            "K": all_Ks,
            "input_indices": list(range(num_inputs + num_targets)),
        }
        # Run rendering.
        num_steps = 50
        options_ori = VERSION_DICT["options"]
        options = copy.deepcopy(options_ori)
        options["chunk_strategy"] = chunk_strategy
        options["video_save_fps"] = 30.0
        options["beta_linear_start"] = 5e-6
        options["log_snr_shift"] = 2.4
        options["guider_types"] = [1, 2]
        options["cfg"] = [
            float(cfg),
            3.0 if num_inputs >= 9 else 2.0,
        ]  # We define semi-dense-view regime to have 9 input views.
        options["camera_scale"] = camera_scale
        options["num_steps"] = num_steps
        options["cfg_min"] = 1.2
        options["encoding_t"] = 1
        options["decoding_t"] = 1
        assert session_hash in ABORT_EVENTS
        abort_event = ABORT_EVENTS[session_hash]
        abort_event.clear()
        options["abort_event"] = abort_event
        task = "img2trajvid"
        # Get number of first pass chunks.
        T_first_pass = T[0] if isinstance(T, (list, tuple)) else T
        chunk_strategy_first_pass = options.get(
            "chunk_strategy_first_pass", "gt-nearest"
        )
        num_chunks_0 = len(
            chunk_input_and_test(
                T_first_pass,
                input_c2ws,
                anchor_c2ws,
                input_indices,
                image_cond["prior_indices"],
                options={**options, "sampler_verbose": False},
                task=task,
                chunk_strategy=chunk_strategy_first_pass,
                gt_input_inds=list(range(input_c2ws.shape[0])),
            )[1]
        )
        # Get number of second pass chunks.
        anchor_argsort = np.argsort(input_indices + anchor_indices).tolist()
        anchor_indices = np.array(input_indices + anchor_indices)[
            anchor_argsort
        ].tolist()
        gt_input_inds = [anchor_argsort.index(i) for i in range(input_c2ws.shape[0])]
        anchor_c2ws_second_pass = torch.cat([input_c2ws, anchor_c2ws], dim=0)[
            anchor_argsort
        ]
        T_second_pass = T[1] if isinstance(T, (list, tuple)) else T
        chunk_strategy = options.get("chunk_strategy", "nearest")
        num_chunks_1 = len(
            chunk_input_and_test(
                T_second_pass,
                anchor_c2ws_second_pass,
                target_c2ws,
                anchor_indices,
                target_indices,
                options={**options, "sampler_verbose": False},
                task=task,
                chunk_strategy=chunk_strategy,
                gt_input_inds=gt_input_inds,
            )[1]
        )
        second_pass_pbar = gr.Progress().tqdm(
            iterable=None,
            desc="Second pass sampling",
            total=num_chunks_1 * num_steps,
        )
        first_pass_pbar = gr.Progress().tqdm(
            iterable=None,
            desc="First pass sampling",
            total=num_chunks_0 * num_steps,
        )
        video_path_generator = run_one_scene(
            task=task,
            version_dict={
                "H": H,
                "W": W,
                "T": T,
                "C": VERSION_DICT["C"],
                "f": VERSION_DICT["f"],
                "options": options,
            },
            model=MODEL,
            ae=AE,
            conditioner=CONDITIONER,
            denoiser=DENOISER,
            image_cond=image_cond,
            camera_cond=camera_cond,
            save_path=render_dir,
            use_traj_prior=True,
            traj_prior_c2ws=anchor_c2ws,
            traj_prior_Ks=anchor_Ks,
            seed=seed,
            gradio=True,
            first_pass_pbar=first_pass_pbar,
            second_pass_pbar=second_pass_pbar,
            abort_event=abort_event,
        )
        
        # Set up a queue for communication from worker thread
        output_queue = queue.Queue()
        
        # We need to capture these to pass to the worker thread
        blocks = LocalContext.blocks.get()
        event_id = LocalContext.event_id.get()
        
        # Initial yield to make download available immediately
        yield (
            gr.update(),  # No video yet
            gr.update(visible=False),  # Hide render button 
            gr.update(visible=True),   # Show abort button
            gr.update(visible=True),   # Show progress
            gr.update(value=zip_path, interactive=True),  # Enable download with initial poses
            None,  # No training output yet
        )

        # Define worker thread to process video generation
        def worker():
            LocalContext.blocks.set(blocks)
            LocalContext.event_id.set(event_id)
            
            try:
                # Use a try-except block specifically for the generator iteration
                # This will catch StopIteration if the generator is aborted
                try:
                    for i, video_path in enumerate(video_path_generator):
                        # Check if abort was requested between iterations
                        if abort_event.is_set():
                            print("Aborting worker during video generation iteration")
                            break
                            
                        if i == 0:
                            # First pass completed - update UI and export with first pass frames
                            self._update_export_with_rendered_frames(
                                osp.join(render_dir, "first-pass"), 
                                export_dir, 
                                num_inputs
                            )
                            first_pass_zip_path = self.create_download_zip(export_dir)
                            
                            training_logs = ""
                            # Update UI with final video and training info
                            output_queue.put({
                                "video": video_path,
                                "render_btn": gr.update(visible=False),
                                "abort_btn": gr.update(visible=True),
                                "progress": gr.update(
                                    visible=True, 
                                    value=f"First pass completed. Processing second pass..."
                                ),
                                "download": gr.update(value=first_pass_zip_path, interactive=True),
                                "training_output": gr.update(visible=False)
                            })
                            if num_frames == 20:
                                return
                        elif i == 1:
                            # Second pass completed - update export with final frames
                            self._update_export_with_rendered_frames(render_dir, export_dir, num_inputs)
                            final_zip_path = self.create_download_zip(export_dir)
                            
                            training_logs = ""
                            # Update UI with final video and training info
                            output_queue.put({
                                "video": video_path,
                                "render_btn": gr.update(visible=True),  # Show render button since completed
                                "abort_btn": gr.update(visible=False), # Hide abort since completed
                                "progress": gr.update(
                                    visible=True, 
                                    value=f"Rendering completed successfully! You can now download the results."
                                ),
                                "download": gr.update(value=final_zip_path, interactive=True),
                                "training_output": gr.update(visible=False)
                            })
                        else:
                            gr.Error("More than two passes during rendering.")
                except StopIteration:
                    print("Video generation was stopped by abort_event")
                    # If aborted during first or second pass, notify the user
                    output_queue.put({
                        "video": None,
                        "render_btn": gr.update(visible=True),
                        "abort_btn": gr.update(visible=False),
                        "progress": gr.update(visible=False, value="Rendering was aborted"),
                        "download": gr.update(value=zip_path, interactive=True),
                        "training_output": gr.update(visible=False)
                    })
                    return
            except Exception as e:
                gr.Error(f"Error during rendering: {str(e)}")
                output_queue.put({
                    "video": None,
                    "render_btn": gr.update(visible=True),
                    "abort_btn": gr.update(visible=False),
                    "progress": gr.update(visible=False),
                    "download": gr.update(value=zip_path, interactive=True),
                    "training_output": gr.update(visible=False)
                })

        # Start worker thread
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        
        # Wait for results or abort with a timeout
        max_wait_time = 300  # 5 minutes max wait time
        wait_start_time = time.time()
        
        while (thread.is_alive() or not output_queue.empty()) and (time.time() - wait_start_time < max_wait_time):
            try:
                # Check if abort was requested
                if abort_event.is_set():
                    print(f"Main render loop detected abort event for {session_hash}")
                    # Give the thread a chance to finish gracefully
                    thread.join(timeout=5.0)
                    if thread.is_alive():
                        print(f"Worker thread didn't exit after 5 seconds, continuing anyway")
                    # Clear abort flag
                    abort_event.clear()
                    # Update UI
                    yield (
                        None,  # Clear video
                        gr.update(visible=True),  # Show render button
                        gr.update(visible=False),  # Hide abort button
                        gr.update(visible=False, value="Rendering was aborted"),  # Hide progress
                        gr.update(value=zip_path, interactive=True),  # Keep download button
                        None,  # No training output yet
                    )
                    return
                
                # Process any available results
                if not output_queue.empty():
                    result = output_queue.get()
                    yield (
                        result["video"],
                        result["render_btn"],
                        result["abort_btn"],
                        result["progress"],
                        result["download"],
                        result["training_output"]
                    )
                
                # Small sleep to prevent tight loop
                time.sleep(0.1)
            except Exception as e:
                print(f"Error in main render loop: {str(e)}")
                # Handle unexpected errors
                yield (
                    None,  # Clear video
                    gr.update(visible=True),  # Show render button
                    gr.update(visible=False),  # Hide abort button
                    gr.update(visible=False, value=f"Error: {str(e)}"),  # Show error message
                    gr.update(value=zip_path, interactive=True),  # Keep download button
                    None,  # No training output yet
                )
                return
        
        # Handle timeout
        if thread.is_alive() and time.time() - wait_start_time >= max_wait_time:
            print(f"Rendering timed out after {max_wait_time} seconds")
            # Try to abort
            abort_event.set()
            thread.join(timeout=1.0)
            abort_event.clear()
            # Update UI
            yield (
                None,  # Clear video
                gr.update(visible=True),  # Show render button
                gr.update(visible=False),  # Hide abort button
                gr.update(visible=False, value="Rendering timed out"),  # Show timeout message
                gr.update(value=zip_path, interactive=True),  # Keep download button
                None,  # No training output yet
            )
            return

    def _update_export_with_rendered_frames(self, render_dir, export_dir, start_idx):
        """Update the export directory with actual rendered frames after they're generated."""
        try:
            # Check if the render directory exists
            if not osp.exists(render_dir):
                gr.Warning(f"Render directory not found: {render_dir}")
                return
            
            # Set the specific samples directory based on whether it's first or second pass
            samples_dir = osp.join(render_dir, "samples-rgb")
            
            # Log for debugging
            gr.Info(f"Updating export from {samples_dir}")
            
            # Check if the samples directory exists
            if not osp.exists(samples_dir):
                gr.Warning(f"Samples directory not found: {samples_dir}")
                return
            
            # Get image files from the samples directory
            image_files = []
            for ext in ["*.png", "*.jpg", "*.jpeg"]:
                image_files.extend(sorted(glob(osp.join(samples_dir, ext))))
            
            if image_files:
                gr.Info(f"Found {len(image_files)} image files")
                
                # Copy the images to the export directory
                for i, frame_path in enumerate(image_files):
                    try:
                        out_path = osp.join(export_dir, f"{start_idx + i:03d}.png")
                        shutil.copy2(frame_path, out_path)
                    except Exception as e:
                        gr.Warning(f"Error copying image {frame_path}: {str(e)}")
                        
                gr.Info(f"Successfully copied {len(image_files)} images to export directory")
            else:
                gr.Warning(f"No image files found in {samples_dir}")
                    
            # Update metadata to mark that frames have been updated
            metadata_path = osp.join(export_dir, "metadata.json")
            if osp.exists(metadata_path):
                with open(metadata_path, "r") as f:
                    metadata = json.load(f)
                    
                metadata["frames_updated"] = True
                metadata["update_timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                metadata["frame_source"] = "images"
                metadata["num_images"] = len(image_files)
                with open(metadata_path, "w") as f:
                    json.dump(metadata, f, indent=4)
                    
        except Exception as e:
            gr.Warning(f"Error updating export with rendered frames: {str(e)}")
            
            
# This is basically a copy of the original `networking.setup_tunnel` function,
# but it also returns the tunnel object for proper cleanup.
def setup_tunnel(
    local_host: str, local_port: int, share_token: str, share_server_address: str | None
) -> tuple[str, Tunnel]:
    share_server_address = (
        networking.GRADIO_SHARE_SERVER_ADDRESS
        if share_server_address is None
        else share_server_address
    )
    if share_server_address is None:
        try:
            response = httpx.get(networking.GRADIO_API_SERVER, timeout=30)
            payload = response.json()[0]
            remote_host, remote_port = payload["host"], int(payload["port"])
            certificate = payload["root_ca"]
            Path(CERTIFICATE_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(CERTIFICATE_PATH, "w") as f:
                f.write(certificate)
        except Exception as e:
            raise RuntimeError(
                "Could not get share link from Gradio API Server."
            ) from e
    else:
        remote_host, remote_port = share_server_address.split(":")
        remote_port = int(remote_port)
    tunnel = Tunnel(remote_host, remote_port, local_host, local_port, share_token)
    address = tunnel.start_tunnel()
    return address, tunnel


def set_bkgd_color(server: viser.ViserServer | viser.ClientHandle):
    server.scene.set_background_image(np.array([[[39, 39, 42]]], dtype=np.uint8))


def start_server_and_abort_event(request: gr.Request):
    server = viser.ViserServer()

    @server.on_client_connect
    def _(client: viser.ClientHandle):
        # Force dark mode that blends well with gradio's dark theme.
        client.gui.configure_theme(
            dark_mode=True,
            show_share_button=False,
            control_layout="collapsible",
        )
        set_bkgd_color(client)

    print(f"Starting server {server.get_port()}")
    server_url, tunnel = setup_tunnel(
        local_host=server.get_host(),
        local_port=server.get_port(),
        share_token=secrets.token_urlsafe(32),
        share_server_address=None,
    )
    SERVERS[request.session_hash] = (server, tunnel)
    if server_url is None:
        raise gr.Error(
            "Failed to get a viewport URL. Please check your network connection."
        )
    # Give it enough time to start.
    time.sleep(1)

    ABORT_EVENTS[request.session_hash] = threading.Event()

    return (
        SevaRenderer(server),
        gr.HTML(
            f'<iframe src="{server_url}" style="display: block; margin: auto; width: 100%; height: min(60vh, 600px);" frameborder="0"></iframe>',
            container=True,
        ),
        request.session_hash,
    )


def stop_server_and_abort_event(request: gr.Request):
    if request.session_hash in SERVERS:
        print(f"Stopping server {request.session_hash}")
        server, tunnel = SERVERS.pop(request.session_hash)
        server.stop()
        tunnel.kill()

    if request.session_hash in ABORT_EVENTS:
        print(f"Setting abort event {request.session_hash}")
        ABORT_EVENTS[request.session_hash].set()
        # Give it enough time to abort jobs.
        time.sleep(5)
        ABORT_EVENTS.pop(request.session_hash)


def set_abort_event(session_hash: str):
    if session_hash in ABORT_EVENTS:
        print(f"Setting abort event for session {session_hash}")
        abort_event = ABORT_EVENTS[session_hash]
        if abort_event.is_set():
            print(f"Abort event was already set for {session_hash}")
        else:
            print(f"Setting abort event flag for {session_hash}")
            abort_event.set()
        
        # Update UI with the abort status
        return [
            gr.update(visible=True),   # Show render button
            gr.update(visible=False),  # Hide abort button
            gr.update(visible=False),  # Hide progress
        ]
    else:
        print(f"Warning: Attempted to set abort event for unknown session hash: {session_hash}")
        
    return [
        gr.update(),  # No change to render button
        gr.update(),  # No change to abort button
        gr.update(),  # No change to progress
    ]


def get_advance_examples(selection: gr.SelectData):
    index = selection.index
    return (
        gr.Gallery(ADVANCE_EXAMPLE_MAP[index][1], visible=True),
        gr.update(visible=True),
        gr.update(visible=True),
        gr.Gallery(visible=False),
    )


def get_preamble():
    gr.Markdown("""
# Stable Virtual Camera
<span style="display: flex; flex-wrap: wrap; gap: 5px;">
    <a href="https://stable-virtual-camera.github.io"><img src="https://img.shields.io/badge/%F0%9F%8F%A0%20Project%20Page-gray.svg"></a>
    <a href="http://arxiv.org/abs/2503.14489"><img src="https://img.shields.io/badge/%F0%9F%93%84%20arXiv-2503.14489-B31B1B.svg"></a>
    <a href="https://stability.ai/news/introducing-stable-virtual-camera-multi-view-video-generation-with-3d-camera-control"><img src="https://img.shields.io/badge/%F0%9F%93%83%20Blog-Stability%20AI-orange.svg"></a>
    <a href="https://huggingface.co/stabilityai/stable-virtual-camera"><img src="https://img.shields.io/badge/%F0%9F%A4%97%20Model_Card-Huggingface-orange"></a>
    <a href="https://huggingface.co/spaces/stabilityai/stable-virtual-camera"><img src="https://img.shields.io/badge/%F0%9F%9A%80%20Gradio%20Demo-Huggingface-orange"></a>
    <a href="https://www.youtube.com/channel/UCLLlVDcS7nNenT_zzO3OPxQ"><img src="https://img.shields.io/badge/%F0%9F%8E%AC%20Video-YouTube-orange"></a>
</span>

Welcome to the demo of <strong>Stable Virtual Camera (Seva)</strong>! Given any number of input views and their cameras, this demo will allow you to generate novel views of a scene at any target camera of interest.

We provide two ways to use our demo (selected by the tab below, documented [here](https://github.com/Stability-AI/stable-virtual-camera/blob/main/docs/GR_USAGE.md)):
1. **[Basic](https://github.com/user-attachments/assets/4d965fa6-d8eb-452c-b773-6e09c88ca705)**: Given a single image, you can generate a video following one of our preset camera trajectories.
2. **[Advanced](https://github.com/user-attachments/assets/dcec1be0-bd10-441e-879c-d1c2b63091ba)**: Given any number of input images, you can generate a video following any camera trajectory of your choice by our key-frame-based interface.

> This is a research preview and comes with a few [limitations](https://stable-virtual-camera.github.io/#limitations):
> - Limited quality in certain subjects due to training data, including humans, animals, and dynamic textures.
> - Limited quality in some highly ambiguous scenes and camera trajectories, including extreme views and collision into objects.
    """)


# Make sure that gradio uses dark theme.
_APP_JS = """
function refresh() {
    const url = new URL(window.location);
    if (url.searchParams.get('__theme') !== 'dark') {
        url.searchParams.set('__theme', 'dark');
    }
}
"""


def main(server_port: int | None = None, share: bool = True):
    with gr.Blocks(js=_APP_JS) as app:
        renderer = gr.State()
        session_hash = gr.State()
        _ = get_preamble()
        with gr.Tabs():
            with gr.Tab("Basic"):
                render_btn = gr.Button("Render video", interactive=False, render=False)
                with gr.Row():
                    with gr.Column():
                        with gr.Group():
                            # Initially disable the Preprocess Images button until an image is selected.
                            preprocess_btn = gr.Button("Preprocess images", interactive=False)
                            preprocess_progress = gr.Textbox(
                                label="",
                                visible=False,
                                interactive=False,
                            )
                        with gr.Group():
                            input_imgs = gr.Image(
                                type="filepath",
                                label="Input",
                                height=200,
                            )
                            _ = gr.Examples(
                                examples=sorted(glob("assets/basic/*")),
                                inputs=[input_imgs],
                                label="Example",
                            )
                            chunk_strategy = gr.Dropdown(
                                ["interp", "interp-gt"],
                                label="Chunk strategy",
                                render=False,
                            )
                            preprocessed = gr.State()
                            # Enable the Preprocess Images button only if an image is selected.
                            input_imgs.change(
                                lambda img: gr.update(interactive=bool(img)),
                                inputs=input_imgs,
                                outputs=preprocess_btn,
                            )
                            preprocess_btn.click(
                                lambda r, *args: [
                                    *r.preprocess(*args),
                                    gr.update(interactive=True),
                                ],
                                inputs=[renderer, input_imgs],
                                outputs=[
                                    preprocessed,
                                    preprocess_progress,
                                    chunk_strategy,
                                    render_btn,
                                ],
                                show_progress_on=[preprocess_progress],
                                concurrency_limit=1,
                                concurrency_id="gpu_queue",
                            )
                            preprocess_btn.click(
                                lambda: gr.update(visible=True),
                                outputs=[preprocess_progress],
                            )
                        with gr.Row():
                            preset_traj = gr.Dropdown(
                                choices=[
                                    "orbit",
                                    "spiral",
                                    "lemniscate",
                                    "zoom-in",
                                    "zoom-out",
                                    "dolly zoom-in",
                                    "dolly zoom-out",
                                    "move-forward",
                                    "move-backward",
                                    "move-up",
                                    "move-down",
                                    "move-left",
                                    "move-right",
                                ],
                                label="Preset trajectory",
                                value="orbit",
                            )
                            num_frames = gr.Slider(20, 150, 80, label="#Frames")
                            zoom_factor = gr.Slider(
                                step=0.01, label="Zoom factor", visible=False
                            )
                        with gr.Row():
                            seed = gr.Number(value=23, label="Random seed")
                            chunk_strategy.render()
                            cfg = gr.Slider(1.0, 7.0, value=4.0, label="CFG value")
                        with gr.Row():
                            camera_scale = gr.Slider(
                                0.1,
                                15.0,
                                value=2.0,
                                label="Camera scale",
                            )

                        def default_cfg_preset_traj(traj):
                            # These are just some hand-tuned values that we
                            # found work the best.
                            if traj in ["zoom-out", "move-down"]:
                                value = 5.0
                            elif traj in [
                                "orbit",
                                "dolly zoom-out",
                                "move-backward",
                                "move-up",
                                "move-left",
                                "move-right",
                            ]:
                                value = 4.0
                            else:
                                value = 3.0
                            return value

                        preset_traj.change(
                            default_cfg_preset_traj,
                            inputs=[preset_traj],
                            outputs=[cfg],
                        )
                        preset_traj.change(
                            lambda traj: gr.update(
                                value=(
                                    10.0 if "dolly" in traj or "pan" in traj else 2.0
                                )
                            ),
                            inputs=[preset_traj],
                            outputs=[camera_scale],
                        )

                        def zoom_factor_preset_traj(traj):
                            visible = traj in [
                                "zoom-in",
                                "zoom-out",
                                "dolly zoom-in",
                                "dolly zoom-out",
                            ]
                            is_zoomin = traj.endswith("zoom-in")
                            if is_zoomin:
                                minimum = 0.1
                                maximum = 0.5
                                value = 0.28
                            else:
                                minimum = 1.2
                                maximum = 3
                                value = 1.5
                            return gr.update(
                                visible=visible,
                                minimum=minimum,
                                maximum=maximum,
                                value=value,
                            )

                        preset_traj.change(
                            zoom_factor_preset_traj,
                            inputs=[preset_traj],
                            outputs=[zoom_factor],
                        )
                    with gr.Column():
                        with gr.Group():
                            abort_btn = gr.Button("Abort rendering", visible=False)
                            render_btn.render()
                            render_progress = gr.Textbox(
                                label="", visible=False, interactive=False
                            )
                        output_video = gr.Video(
                            label="Output", interactive=False, autoplay=True, loop=True
                        )
                        with gr.Group():
                            output_data_btn = gr.Button("Download Results as ZIP")
                            download_btn = gr.File(label="Download NeRF/Gaussian Splatting Data", interactive=False)
                            training_output = gr.Textbox(label="Nerfstudio Training", interactive=False, visible=False)
                        output_data_btn.click(
                            lambda r, *args: r.export_output_data(*args),
                            inputs=[renderer, preprocessed],
                            outputs=[download_btn],
                        )
                        render_btn.click(
                            lambda r, *args: (yield from r.render(*args)),
                            inputs=[
                                renderer,
                                preprocessed,
                                session_hash,
                                seed,
                                chunk_strategy,
                                cfg,
                                preset_traj,
                                num_frames,
                                zoom_factor,
                                camera_scale,
                            ],
                            outputs=[
                                output_video,
                                render_btn,
                                abort_btn,
                                render_progress,
                                download_btn,
                                training_output,
                            ],
                            show_progress_on=[render_progress],
                            concurrency_id="gpu_queue",
                        )
                        render_btn.click(
                            lambda: [
                                gr.update(visible=False),
                                gr.update(visible=True),
                                gr.update(visible=True),
                                gr.update(interactive=True),
                                gr.update(visible=False),
                            ],
                            outputs=[render_btn, abort_btn, render_progress, download_btn, training_output],
                        )
                        abort_btn.click(
                            set_abort_event,
                            inputs=[session_hash],
                            outputs=[render_btn, abort_btn, render_progress]
                        )
            with gr.Tab("Advanced"):
                render_btn = gr.Button("Render video", interactive=False, render=False)
                viewport = gr.HTML(container=True, render=False)
                gr.Timer(0.1).tick(
                    lambda renderer: gr.update(
                        interactive=renderer is not None
                        and renderer.gui_state is not None
                        and renderer.gui_state.camera_traj_list is not None
                    ),
                    inputs=[renderer],
                    outputs=[render_btn],
                )
                with gr.Row():
                    viewport.render()
                with gr.Row():
                    with gr.Column():
                        with gr.Group():
                            # Initially disable the Preprocess Images button until images are selected.
                            preprocess_btn = gr.Button("Preprocess images", interactive=False)
                            preprocess_progress = gr.Textbox(
                                label="",
                                visible=False,
                                interactive=False,
                            )
                        with gr.Group():
                            input_imgs = gr.Gallery(
                                interactive=True,
                                label="Input",
                                columns=4,
                                height=200,
                            )
                            # Define example images (gradio doesn't support variable length
                            # examples so we need to hack it).
                            example_imgs = gr.Gallery(
                                [e[0] for e in ADVANCE_EXAMPLE_MAP],
                                allow_preview=False,
                                preview=False,
                                label="Example",
                                columns=20,
                                rows=1,
                                height=115,
                            )
                            example_imgs_expander = gr.Gallery(
                                visible=False,
                                interactive=False,
                                label="Example",
                                preview=True,
                                columns=20,
                                rows=1,
                            )
                            chunk_strategy = gr.Dropdown(
                                ["interp-gt", "interp"],
                                label="Chunk strategy",
                                value="interp-gt",
                                render=False,
                            )
                            with gr.Row():
                                example_imgs_backer = gr.Button(
                                    "Go back", visible=False
                                )
                                example_imgs_confirmer = gr.Button(
                                    "Confirm", visible=False
                                )
                            example_imgs.select(
                                get_advance_examples,
                                outputs=[
                                    example_imgs_expander,
                                    example_imgs_confirmer,
                                    example_imgs_backer,
                                    example_imgs,
                                ],
                            )
                            example_imgs_confirmer.click(
                                lambda x: (
                                    x,
                                    gr.update(visible=False),
                                    gr.update(visible=False),
                                    gr.update(visible=False),
                                    gr.update(visible=True),
                                    gr.update(interactive=bool(x))
                                ),
                                inputs=[example_imgs_expander],
                                outputs=[
                                    input_imgs,
                                    example_imgs_expander,
                                    example_imgs_confirmer,
                                    example_imgs_backer,
                                    example_imgs,
                                    preprocess_btn
                                ],
                            )
                            example_imgs_backer.click(
                                lambda: (
                                    gr.update(visible=False),
                                    gr.update(visible=False),
                                    gr.update(visible=False),
                                    gr.update(visible=True),
                                ),
                                outputs=[
                                    example_imgs_expander,
                                    example_imgs_confirmer,
                                    example_imgs_backer,
                                    example_imgs,
                                ],
                            )
                            preprocessed = gr.State()
                            # Enable the Preprocess Images button when images are uploaded
                            input_imgs.change(
                                lambda imgs: gr.update(interactive=bool(imgs)),
                                inputs=[input_imgs],
                                outputs=[preprocess_btn],
                            )
                            preprocess_btn.click(
                                lambda r, *args: r.preprocess(*args),
                                inputs=[renderer, input_imgs],
                                outputs=[
                                    preprocessed,
                                    preprocess_progress,
                                    chunk_strategy,
                                ],
                                show_progress_on=[preprocess_progress],
                                concurrency_id="gpu_queue",
                            )
                            preprocess_btn.click(
                                lambda: gr.update(visible=True),
                                outputs=[preprocess_progress],
                            )
                            preprocessed.change(
                                lambda r, *args: r.visualize_scene(*args),
                                inputs=[renderer, preprocessed],
                            )
                        with gr.Row():
                            seed = gr.Number(value=23, label="Random seed")
                            chunk_strategy.render()
                            cfg = gr.Slider(1.0, 7.0, value=3.0, label="CFG value")
                        with gr.Row():
                            camera_scale = gr.Slider(
                                0.1,
                                15.0,
                                value=2.0,
                                label="Camera scale (useful for single-view input)",
                            )
                        with gr.Group():
                            output_data_btn = gr.Button("Download Results as ZIP")
                            download_btn = gr.File(label="Download NeRF/Gaussian Splatting Data", interactive=False)
                            training_output = gr.Textbox(label="Nerfstudio Training", interactive=False, visible=False)
                        output_data_btn.click(
                            lambda r, *args: r.export_output_data(*args),
                            inputs=[renderer, preprocessed],
                            outputs=[download_btn],
                        )
                        render_btn.click(
                            lambda r, *args: (yield from r.render(*args)),
                            inputs=[
                                renderer,
                                preprocessed,
                                session_hash,
                                seed,
                                chunk_strategy,
                                cfg,
                                preset_traj,
                                num_frames,
                                zoom_factor,
                                camera_scale,
                            ],
                            outputs=[
                                output_video,
                                render_btn,
                                abort_btn,
                                render_progress,
                                download_btn,
                                training_output,
                            ],
                            show_progress_on=[render_progress],
                            concurrency_id="gpu_queue",
                        )
                        render_btn.click(
                            lambda: [
                                gr.update(visible=False),
                                gr.update(visible=True),
                                gr.update(visible=True),
                                gr.update(interactive=True),
                                gr.update(visible=False),
                            ],
                            outputs=[render_btn, abort_btn, render_progress, download_btn, training_output],
                        )
                        abort_btn.click(
                            set_abort_event,
                            inputs=[session_hash],
                            outputs=[render_btn, abort_btn, render_progress]
                        )

        # Register the session initialization and cleanup functions.
        app.load(
            start_server_and_abort_event,
            outputs=[renderer, viewport, session_hash],
        )
        app.unload(stop_server_and_abort_event)

    app.queue(max_size=5).launch(
        share=share,
        server_port=server_port,
        show_error=True,
        allowed_paths=[WORK_DIR],
        # Badget rendering will be broken otherwise.
        ssr_mode=False,
    )


if __name__ == "__main__":
    tyro.cli(main)
