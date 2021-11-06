import argparse
import os

import numpy
import torch
import torchvision
from torch import optim
from tqdm import tqdm
from criteria.clip_loss import CLIPLoss
from models.stylegan2.model import Generator
import math


def get_latent(g_ema):
    mean_latent = g_ema.module.mean_latent(4096)
    latent_code_init_not_trunc = torch.randn(1, 512).cuda()
    with torch.no_grad():
        _, latent_code_init, _ = g_ema([latent_code_init_not_trunc], return_latents=True,
                                       truncation=args.truncation, truncation_latent=mean_latent)

    direction = latent_code_init.detach().clone()
    direction.requires_grad = True
    return direction


def load_model(args):
    g_ema = Generator(args.stylegan_size, 512, 8)
    g_ema.load_state_dict(torch.load(args.ckpt)["g_ema"], strict=False)
    g_ema.eval()
    g_ema = torch.nn.DataParallel(g_ema)
    g_ema = g_ema.cuda()
    return g_ema


def get_lr(t, initial_lr, rampdown=0.75, rampup=0.005):
    lr_ramp = min(1, (1 - t) / rampdown)
    lr_ramp = 0.5 - 0.5 * math.cos(lr_ramp * math.pi)
    lr_ramp = lr_ramp * min(1, t / rampup)

    return initial_lr * lr_ramp


def main(args):
    g_ema = load_model(args)
    n=0
    dir_name = args.dir_name

    if not os.path.exists(dir_name):
        os.mkdir(dir_name)

    NUM_DIRECTIONS = args.num_directions
    NUM_IMAGES = args.num_images

    if args.dir_initialization is None:
        directions = [get_latent(g_ema) for _ in range(NUM_DIRECTIONS)]
        directions_cat = torch.cat(directions)
    else:
        target_dir = torch.load(args.dir_initialization).reshape(1, -1, 512).detach().clone().cuda()
        target_dir.requires_grad = True
        directions = [target_dir]
        directions_cat = target_dir.expand(NUM_DIRECTIONS, target_dir.shape[1], target_dir.shape[2])

    with torch.no_grad():
        dirs, _ = g_ema([directions_cat], input_is_latent=True, randomize_noise=False)
        for j, latent in enumerate(directions):
            torchvision.utils.save_image(dirs[j], f"{dir_name}/dir_{j}.png", normalize=True, range=(-1, 1))

        latents = [None] * NUM_IMAGES
        if args.generated_images:
            for n in range(NUM_IMAGES):
                with torch.no_grad():
                    latents[n] = get_latent(g_ema)
                    latents[n].requires_grad = False
        else:
            data = torch.load(args.data_path)
            for n in range(NUM_IMAGES):
                with torch.no_grad():###############################################
                    latents[n] = data[n+3].unsqueeze(0).cuda()
                    latents[n].requires_grad = False
        latents = torch.cat(latents)

    clip_loss = CLIPLoss(args)
    clip_loss = torch.nn.DataParallel(clip_loss)
    optimizer = optim.Adam(directions, lr=args.lr, weight_decay=args.weight_decay)

    import torchvision.transforms as transforms
    from PIL import Image
    transform = transforms.Compose([
        transforms.Resize((args.stylegan_size, args.stylegan_size)),
        transforms.ToTensor(),
        transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])])

    with torch.no_grad():
        targets_clip = None
        if args.target_path is not None:
            tgt_lat = None
            # target is image from file
            img_target = Image.open(args.target_path)
            crop_size = min(img_target.size[0], img_target.size[1])
            crop = transforms.Compose([
                transforms.CenterCrop(crop_size)])
            img_target = crop(img_target)
            img_target = transform(img_target).unsqueeze(0).cuda()
            torchvision.utils.save_image(img_target, f"{dir_name}/target.png",
                                         normalize=True, range=(-1, 1))
            target_clip = clip_loss.module.encode(img_target)
            target_clip = target_clip / target_clip.norm(dim=-1)
            target_clip.requires_grad = False
        else:
            # target is latent dir
            with torch.no_grad():
                img_target, _ = g_ema([directions_cat], input_is_latent=True, randomize_noise=False)
                targets_clip = clip_loss.module.encode(img_target)
                targets_clip.requires_grad = False

    for dir_idx, direction in enumerate(directions):

        with torch.no_grad():
            if targets_clip is not None:
                target_clip = targets_clip[dir_idx]
                target_clip = target_clip / target_clip.norm(dim=-1)

        coefficients = [None] * NUM_IMAGES
        for n in range(NUM_IMAGES):
            coefficient = torch.ones(1).to("cuda")
            coefficient.requires_grad = True
            coefficients[n] = coefficient

        opt_loss = torch.Tensor([float("Inf")]).to("cuda")
        pbar = tqdm(range(args.step))

        for i in pbar:
            # calculate learning rate
            t = i / args.step
            lr = get_lr(t, args.lr)
            optimizer.param_groups[0]["lr"] = lr

            optimizer_coeffs = optim.Adam(coefficients, lr=args.lr, weight_decay=0.01)
            loss = torch.zeros(1).cuda()
            target_semantic = torch.zeros(1).cuda()

            with torch.no_grad():
                img_gen, _ = g_ema([latents], input_is_latent=True, randomize_noise=False)
                image_gen_clip = clip_loss.module.encode(img_gen)

            direction_with_coeff = [direction * coefficients[i] for i in range(args.num_images)]
            direction_with_coeff = torch.stack(direction_with_coeff).squeeze(1).cuda()
            img_gen_amp, _ = g_ema([latents + direction_with_coeff], input_is_latent=True, randomize_noise=False)

            image_gen_amp_clip = clip_loss.module.encode(img_gen_amp)
            diffs = image_gen_amp_clip - image_gen_clip
            diffs = diffs / diffs.norm(dim=-1, keepdim=True)

            # transfer loss
            image_gen_amp_clip_norm = image_gen_amp_clip / image_gen_amp_clip.norm(dim=-1, keepdim=True)
            similarity_gap = image_gen_amp_clip_norm @ target_clip.T
            target_semantic += 1 - similarity_gap.mean()

            if args.lambda_consistency > 0:
                diffs_mat_amp = diffs @ diffs.T
                ones_mat = torch.ones(diffs_mat_amp.shape[0]).cuda()
                similarities_loss = torch.sum(ones_mat - diffs_mat_amp) / (NUM_IMAGES ** 2 - NUM_IMAGES)
                loss += args.lambda_consistency * similarities_loss

            # add semantic transfer loss
            loss += args.lambda_transfer * target_semantic.reshape(loss.shape)

            pbar.set_description(
                (
                    f"loss: {loss.item():.4f}; consistency loss: {similarities_loss.view(-1).item():.4f};"
                    f"transfer loss: {target_semantic.item():.4f}; "
                    f"lr: {lr:.4f}; norm: {direction.norm().item():.4f}"
                )
            )

            optimizer.zero_grad()
            optimizer_coeffs.zero_grad()
            loss.backward()
            optimizer_coeffs.step()
            optimizer.step()

            with torch.no_grad():

                if loss < opt_loss:
                    
                    print("saved="+str(n))
                    numpy.save('{0}/direction{1}.npy'.format(args.dir_name, dir_idx),
                               direction.detach().cpu().numpy())
                    opt_loss = loss
                    img_gen, _ = g_ema([latents], input_is_latent=True, randomize_noise=False)
                    direction_with_coeff = [direction * coefficients[i] for i in range(args.num_images)]
                    direction_with_coeff = torch.stack(direction_with_coeff).squeeze(1).cuda()
                    img_gen_amp, _ = g_ema([latents + direction_with_coeff], input_is_latent=True,
                                           randomize_noise=False)

                    for j in range(args.num_images):
                        torchvision.utils.save_image(img_gen[j], f"{dir_name}/img_gen_{j}.png",
                                                     normalize=True, range=(-1, 1))
                        torchvision.utils.save_image(img_gen_amp[j],
                                                     f"{dir_name}/img_gen_amp_{dir_idx}_{j}.png",
                                                     normalize=True, range=(-1, 1))

                   n+=1
if __name__ == "__main__":
    torch.manual_seed(12345)
    parser = argparse.ArgumentParser()
    parser.add_argument("--lambda_consistency", type=float, default=0.6)
    parser.add_argument("--dir_name", type=str, default="results_transfer",
                        help="name of directory to store results")
    parser.add_argument("--ckpt", type=str, default="./pretrained_models/stylegan2-ffhq-config-f.pt",
                        help="pretrained StyleGAN2 weights")
    parser.add_argument("--stylegan_size", type=int, default=1024, help="StyleGAN resolution")
    parser.add_argument("--lr", type=float, default=0.2)
    parser.add_argument("--weight_decay", type=float, default=3e-3)
    parser.add_argument("--step", type=int, default=1000, help="number of optimization steps")
    parser.add_argument("--target_path", type=str, default=None,
                        help="starts the optimization from the given latent code if provided. Otherwose, starts from"
                             "the mean latent in a free generation, and from a random one in editing. "
                             "Expects a .pt format")
    parser.add_argument("--truncation", type=float, default=0.7,
                        help="used only for the initial latent vector, and only when a latent code path is"
                             "not provided")
    parser.add_argument("--save_intermediate_image_every", type=int, default=1,  # 150,
                        help="if > 0 then saves intermidate results during the optimization")
    parser.add_argument("--results_dir", type=str, default="results")
    parser.add_argument("--lambda_transfer", type=float, default=1)
    parser.add_argument("--num_images", type=int, default=8, help="Number of training images")
    parser.add_argument("--num_directions", type=int, default=10, help="number of directions to try")
    parser.add_argument("--generated_images", default=False, action='store_true')
    parser.add_argument("--data_path", type=str, default="pretrained_models/test_faces.pt")
    parser.add_argument("--dir_initialization", type=str, default=None)

    args = parser.parse_args()

    result_image = main(args)
