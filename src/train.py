#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import torch
from torch.utils.data import DataLoader
from random import randint
from utils.loss_utils import l1_loss, ssim
from gaussian_renderer import render, render_flow, network_gui
import sys
from scene import Scene, GaussianModel
from utils.general_utils import safe_state
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams
import copy
try:
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False

from PIL import Image
import numpy as np

def training(dataset, opt, pipe, testing_iterations, saving_iterations, checkpoint_iterations, checkpoint, debug_from):
    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    gaussians = GaussianModel(dataset.sh_degree, dataset.approx_l)
    scene = Scene(dataset, gaussians)
    gaussians.training_setup(opt)
    if checkpoint:
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, opt)


    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")
    white_color = [1, 1, 1]
    white_background = torch.tensor(white_color, dtype=torch.float32, device="cuda")

    iter_start = torch.cuda.Event(enable_timing = True)
    iter_end = torch.cuda.Event(enable_timing = True)

    viewpoint_stack = None
    ema_loss_for_log = 0.0
    ema_flow_loss_for_log = 0.0
    progress_bar = tqdm(range(first_iter, opt.iterations), desc="Training progress")
    first_iter += 1
    if scene.use_loader:
        cam_loader = DataLoader(scene.getTrainCameras(), batch_size=1, shuffle=True, num_workers=16, collate_fn=list)
        loader = iter(cam_loader)

    # mask = torch.tensor((np.array(Image.open('mask.png'))[:, :, 3] > 0)).cuda()

    for iteration in range(first_iter, opt.iterations + 1):
        torch.cuda.empty_cache()

        if network_gui.conn == None:
            network_gui.try_connect()
        while network_gui.conn != None:
            torch.cuda.empty_cache()

            try:
                net_image_bytes = None
                custom_cam, do_training, pipe.convert_SHs_python, pipe.compute_cov3D_python, keep_alive, scaling_modifer = network_gui.receive()
                if custom_cam != None:
                    net_image = render(custom_cam, gaussians, pipe, background, scaling_modifer)["render"]
                    net_image_bytes = memoryview((torch.clamp(net_image, min=0, max=1.0) * 255).byte().permute(1, 2, 0).contiguous().cpu().numpy())
                network_gui.send(net_image_bytes, dataset.source_path)
                if do_training and ((iteration < int(opt.iterations)) or not keep_alive):
                    break
            except Exception as e:
                network_gui.conn = None

        iter_start.record()

        gaussians.update_learning_rate(iteration)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()


        if scene.use_loader:
            try:
                viewpoint_cam = next(loader)[0]
            except:
                loader = iter(cam_loader)
                viewpoint_cam = next(loader)[0]
        else:
            # Pick a random Camera
            if not viewpoint_stack:
                viewpoint_stack = scene.getTrainCameras().copy()
            viewpoint_cam = viewpoint_stack.pop(randint(0, len(viewpoint_stack)-1))

        # Render
        if (iteration - 1) == debug_from:
            pipe.debug = True


        render_pkg = render(viewpoint_cam, gaussians, pipe, background, itr=iteration)
        image, viewspace_point_tensor, visibility_filter, radii = render_pkg["render"], render_pkg["viewspace_points"], render_pkg["visibility_filter"], render_pkg["radii"]
        # Loss
        gt_image = viewpoint_cam.original_image.cuda()

        # if iteration % 200 == 0:
            # print(image.mean().item(), image.max().item(), image.min().item(), gt_image.mean().item(), gt_image.max().item(), gt_image.min().item())
        Ll1 = l1_loss(image, gt_image)
        # Ll1 = (torch.abs((image - gt_image))*mask).mean()
        lasso_loss = torch.nanmean(torch.abs(gaussians.get_xyz[:, 1:, :])) # + torch.nanmean(torch.abs(gaussians.get_rotation[:, 1:, :]))
        # print(lasso_loss, gaussians.get_rotation[:, 1:, :].abs().mean())
        flow_loss = 0
        if iteration >= 3000 and viewpoint_cam.kwargs['fwd_flow'] is not None:
            fwd_flow = viewpoint_cam.kwargs['fwd_flow'].cuda().permute(2, 0, 1)
            fwd_flow_mask = viewpoint_cam.kwargs['fwd_flow_mask'].cuda()
            render_flow_fwd = render_flow(viewpoint_cam, gaussians, pipe, white_background, itr=iteration, time_delta=scene.time_delta)['render'][:2, ...]
            fwd_flow = fwd_flow / (torch.max(torch.sqrt(torch.square(fwd_flow).sum(-1))) + 1e-5)
            render_flow_fwd = render_flow_fwd / (torch.max(torch.sqrt(torch.square(render_flow_fwd).sum(-1))) + 1e-5)
            M = fwd_flow_mask.unsqueeze(0)
            fwd_flow_loss = torch.sum(torch.abs(fwd_flow - render_flow_fwd) * M) / (torch.sum(M) + 1e-8) / fwd_flow.shape[-1]
            # fwd_flow_loss = torch.mean(torch.abs(fwd_flow - render_flow_fwd))
            flow_loss += fwd_flow_loss

        if iteration >= 3000 and viewpoint_cam.kwargs['bwd_flow'] is not None:
            bwd_flow = viewpoint_cam.kwargs['bwd_flow'].permute(2, 0, 1).cuda()
            bwd_flow_mask = viewpoint_cam.kwargs['bwd_flow_mask'].cuda()
            render_flow_bwd = render_flow(viewpoint_cam, gaussians, pipe, white_background, itr=iteration, time_delta=-scene.time_delta)['render'][:2, ...]
            bwd_flow = bwd_flow / (torch.max(torch.sqrt(torch.square(bwd_flow).sum(-1))) + 1e-5)
            render_flow_bwd = render_flow_bwd / (torch.max(torch.sqrt(torch.square(render_flow_bwd).sum(-1))) + 1e-5)
            M = bwd_flow_mask.unsqueeze(0)
            bwd_flow_loss = torch.sum(torch.abs(bwd_flow - render_flow_bwd) * M) / (torch.sum(M) + 1e-8) / bwd_flow.shape[-1]
            flow_loss += bwd_flow_loss

        loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (1.0 - ssim(image, gt_image)) + opt.lambda_lasso * lasso_loss
        loss += opt.lambda_flow * flow_loss
        # if type(loss) is int:
            # continue
        loss.backward()
        # print(gaussians._xyz[:, 0, 0].grad.shape)
        # print(gaussians.optimizer.param_groups[0]['params'][0].data.abs().mean(0)[1:5].flatten())
        iter_end.record()

        with torch.no_grad():
            # Progress bar
            torch.cuda.empty_cache()

            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log
            fl = flow_loss.item() if hasattr(flow_loss, 'item') else flow_loss
            ema_flow_loss_for_log = 0.4 * fl + 0.6 * ema_flow_loss_for_log
            if iteration % 10 == 0:
                progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{7}f}", "Flow loss": f"{ema_flow_loss_for_log:.{7}f}"})
                progress_bar.update(10)
            if iteration == opt.iterations:
                progress_bar.close()

            # Log and save
            training_report(tb_writer, iteration, Ll1, loss, l1_loss, iter_start.elapsed_time(iter_end), testing_iterations, scene, render, (pipe, background))
            if (iteration in saving_iterations):
                print("\n[ITER {}] Saving Gaussians".format(iteration))
                scene.save(iteration)

            # Densification
            if iteration < opt.densify_until_iter:
                # Keep track of max radii in image-space for pruning
                gaussians.max_radii2D[visibility_filter] = torch.max(gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)

                if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
                    size_threshold = 20 if iteration > opt.opacity_reset_interval else None
                    gaussians.densify_and_prune(opt.densify_grad_threshold, 0.005, scene.cameras_extent, size_threshold)
                
                if iteration % opt.opacity_reset_interval == 0 or (dataset.white_background and iteration == opt.densify_from_iter):
                    gaussians.reset_opacity()

            # Optimizer step
            if iteration < opt.iterations:
                gaussians.optimizer.step()
                gaussians.optimizer.zero_grad(set_to_none = True)

            if (iteration in checkpoint_iterations):
                print("\n[ITER {}] Saving Checkpoint".format(iteration))
                torch.save((gaussians.capture(), iteration), scene.model_path + "/chkpnt" + str(iteration) + ".pth")

def prepare_output_and_logger(args):    
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])
        
    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer

def training_report(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, testing_iterations, scene : Scene, renderFunc, renderArgs):
    if tb_writer:
        tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iteration)
        tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iteration)
        tb_writer.add_scalar('iter_time', elapsed, iteration)

    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = ({'name': 'test', 'cameras' : scene.getTestCameras()}, 
                              {'name': 'train', 'cameras' : [scene.getTrainCameras()[idx % len(scene.getTrainCameras())] for idx in range(5, 30, 5)]})

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    image = torch.clamp(renderFunc(viewpoint, scene.gaussians, *renderArgs)["render"], 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(config['name'] + "_view_{}/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        if iteration == testing_iterations[0]:
                            tb_writer.add_images(config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])          
                print("\n[ITER {}] Evaluating {}: L1 {} PSNR {}".format(iteration, config['name'], l1_test, psnr_test))
                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)

        if tb_writer:
            tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iteration)
            tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iteration)
        torch.cuda.empty_cache()

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument("--test_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--start_checkpoint", type=str, default = None)
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)
    
    print("Optimizing " + args.model_path)

    # Initialize system state (RNG)
    safe_state(args.quiet)

    # Start GUI server, configure and run training
    network_gui.init(args.ip, args.port)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)
    print("Emptying cache")
    torch.cuda.empty_cache()
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:128"
    training(lp.extract(args), op.extract(args), pp.extract(args), args.test_iterations, args.save_iterations, args.checkpoint_iterations, args.start_checkpoint, args.debug_from)

    # All done
    print("\nTraining complete.")
