import math
from operator import mod
import os

import gradio as gr
import torch

from modules import images, processing, prompt_parser, scripts, shared
from modules.processing import Processed, process_images
from modules.shared import cmd_opts, opts, state


def n_evenly_spaced(a, n):
    res = [a[math.ceil(i/(n-1) * (len(a)-1))] for i in range(n)]
    return res

# build prompt with weights scaled by t
def prompt_at_t(weight_indexes, prompt_list, t):
    return " AND ".join(
        [
            ":".join((prompt_list[index], str(weight * t)))
            for index, weight in weight_indexes
        ]
    )

# used to set denoise strength and cfg scaled by t 
# TODO: options for sigmoid, parabolic, others? instead of linear
def lerp_at_t(min_val, max_val, t):
    diff = max_val - min_val
    return ( min_val + ( t * diff ) )

"""
Interpolate between two (or more) prompts and create an image at each step.
"""
class Script(scripts.Script):
    def title(self):
        return "Prompt morph"

    def show(self, is_img2img):
        return not is_img2img

    def ui(self, is_img2img):
        i1 = gr.HTML("<p style=\"margin-bottom:0.75em\">Keyframe Format: <br>Seed | Prompt or just Prompt</p>")
        #TODO: Figure out elegant way to select txt2img or img2img separately for each pair of prompts.
        #Also TODO: Figure out how to do tooltips instead of putting walls of text in the label
        mode_select = gr.Radio(choices=["txt2img morph", "img2img morph"], value="txt2img morph", label="Type of morph to generate.\n\
                                                                                                         txt2img morph will interpolate between two independently generated images - good for morphing between two pics with very different subjects.\n\
                                                                                                         img2img morph will generate one image, then interpolate between the original and an img2img derivative of it - good for maintaining a consistent subject with only smaller details changed.")
        prompt_list = gr.TextArea(label="Prompt list", placeholder="Enter one prompt per line. Blank lines will be ignored.")
        negative_prompt_list = gr.TextArea(label="Negative prompt list", placeholder="Enter negative prompts if desired, one per line. If number of positive prompts is greater than the number of negative prompts, the last negative prompt (if any) will be applied to remaining positive prompts.")
        n_images = gr.Slider(minimum=2, maximum=256, value=25, step=1, label="Number of images between keyframes")
        save_video = gr.Checkbox(label='Save results as video', value=True)
        video_fps = gr.Number(label='Frames per second', value=5)
        #TODO: Set i2i parameters visible/interactive based on mode selected
        #TODO: Force min_denoise_str <= max_denoise_str, and min_i2i_cfg <= p.cfg
        min_denoise_str = gr.Slider(minimum=0.0, maximum=1.0, value=0.4, step=0.01, label="Denoise strength for first step. Ignored if only 2 images requested.")
        max_denoise_str = gr.Slider(minimum=0.0, maximum=1.0, value=0.9, step=0.01, label="Denoise strength for generating target image.")
        alt_i2i_cfg = gr.Checkbox(value=False, label="Use alternate CFG scale for img2img gens")
        gradual_cfg = gr.Checkbox(value=False, label="Gradually increase CFG scale - helps with preventing artifacting/'deepfrying' on img2img gens with low denoising strength.")
        min_i2i_cfg = gr.Slider(minimum=1, maximum=30, value=7, label="CFG scale for first img2img step (ignored if only 2 images requested)")
        max_i2i_cfg = gr.Slider(minimum=1, maximum=30, value=7, label="CFG scale for final img2img step (used for all i2i gens if gradual CFG not selected)")
        return [i1, prompt_list, negative_prompt_list, mode_select, n_images, save_video, video_fps, min_denoise_str, max_denoise_str, alt_i2i_cfg, gradual_cfg, min_i2i_cfg, max_i2i_cfg]

    def run(self, p, i1, prompt_list, negative_prompt_list, mode_select, n_images, save_video, video_fps, min_denoise_str, max_denoise_str, alt_i2i_cfg, gradual_cfg, min_i2i_cfg, max_i2i_cfg):
        # override batch count and size
        p.batch_size = 1
        p.n_iter = 1

        prompts = []
        negative_prompts = []
        for line in prompt_list.splitlines():
            line = line.strip()
            if line == '':
                continue
            prompt_args = line.split('|')
            if len(prompt_args) == 1:  # no args
                seed, prompt = '', prompt_args[0]
            else:
                seed, prompt = prompt_args
            prompts.append((seed.strip(), prompt.strip()))

        if len(negative_prompt_list.splitlines()) > 0:
            for line in negative_prompt_list.splitlines():
                line = line.strip()
                if line == '':
                    continue
                negative_prompts.append(line)
        else:
            #iffy on this - does it make more sense to grab the normal negative prompt field or to ignore it?
            negative_prompts.append(p.negative_prompt)
        while len(negative_prompts) < len(prompts):
                negative_prompts.append(negative_prompts[-1])

        if len(prompts) < 2:
            msg = "prompt_morph: at least 2 prompts required"
            print(msg)
            return Processed(p, [], p.seed, info=msg)

        state.job_count = 1 + (n_images - 1) * (len(prompts) - 1)

        if save_video:
            import numpy as np
            try:
                import moviepy.video.io.ImageSequenceClip as ImageSequenceClip
            except ImportError:
                msg = "moviepy python module not installed. Will not be able to generate video."
                print(msg)
                return Processed(p, [], p.seed, info=msg)

        # TODO: use a timestamp instead
        # write images to a numbered folder in morphs
        morph_path = os.path.join(p.outpath_samples, "morphs")
        os.makedirs(morph_path, exist_ok=True)
        morph_number = images.get_next_sequence_number(morph_path, "")
        morph_path = os.path.join(morph_path, f"{morph_number:05}")
        p.outpath_samples = morph_path

        all_images = []

        if not alt_i2i_cfg:
            max_i2i_cfg = p.cfg_scale
        i2i_p = processing.StableDiffusionProcessingImg2Img(
            sd_model=p.sd_model,
            outpath_samples=p.outpath_samples,
            outpath_grids=p.outpath_grids,
            styles=p.styles,
            seed=p.seed,
            subseed=p.subseed,
            subseed_strength=p.subseed_strength,
            seed_resize_from_h=p.seed_resize_from_h,
            seed_resize_from_w=p.seed_resize_from_w,
            sampler_index=p.sampler_index,
            batch_size=1,
            n_iter=1,
            steps=p.steps,
            cfg_scale=max_i2i_cfg,
            width=p.width,
            height=p.height,
            restore_faces=p.restore_faces,
            tiling=p.tiling,
            init_images=[]
        )

        for n in range(1, len(prompts)):
            # parsed prompts
            start_seed, start_prompt = prompts[n-1]
            start_neg_prompt = negative_prompts[n-1]
            target_seed, target_prompt = prompts[n]
            target_neg_prompt = negative_prompts[n]
            res_indexes, prompt_flat_list, prompt_indexes = prompt_parser.get_multicond_prompt_list([start_prompt, target_prompt])
            neg_res_indexes, neg_prompt_flat_list, neg_prompt_indexes = prompt_parser.get_multicond_prompt_list([start_neg_prompt, target_neg_prompt])
            prompt_weights, target_weights = res_indexes
            neg_prompt_weights, neg_target_weights = neg_res_indexes

            # fix seeds. interpret '' as use previous seed
            if start_seed != '':
                if start_seed == '-1':
                    start_seed = -1
                p.seed = start_seed
            processing.fix_seed(p)

            if target_seed == '':
                p.subseed = p.seed
                i2i_p.seed = p.seed
            else:
                if target_seed == '-1':
                    target_seed = -1
                p.subseed = target_seed
                i2i_p.seed = target_seed
            processing.fix_seed(i2i_p)
            p.subseed_strength = 0

            # one image for each interpolation step (including start and end)
            for i in range(n_images):
                # first image is same as last of previous morph
                if i == 0 and n > 1:
                    if mode_select == "img2img morph":
                        i2i_p.init_images[0] = all_images[-1]
                    continue
                state.job = f"Morph {n}/{len(prompts)-1}, image {i+1}/{n_images}"

                # TODO: optimize when weight is zero
                # update prompt weights and subseed strength
                t = i / (n_images - 1)
                scaled_prompt = prompt_at_t(prompt_weights, prompt_flat_list, 1.0 - t)
                scaled_target = prompt_at_t(target_weights, prompt_flat_list, t)
                scaled_negative_prompt = prompt_at_t(neg_prompt_weights, neg_prompt_flat_list, 1.0 - t)
                scaled_negative_target = prompt_at_t(neg_target_weights, neg_prompt_flat_list, t)
                p.prompt = f'{scaled_prompt} AND {scaled_target}'
                p.negative_prompt = f'{scaled_negative_prompt} AND {scaled_negative_target}'
                if p.seed != p.subseed and mode_select == "txt2img morph":
                    p.subseed_strength = t
                
                if i == 0 or mode_select != "img2img morph":
                    processed = process_images(p)
                else:
                    i2i_p.prompt = p.prompt
                    i2i_p.negative_prompt = p.negative_prompt
                    if n_images > 2:
                        i2i_t = (i - 1) / (n_images - 2)
                    else:
                        i2i_t = 1.0
                    if gradual_cfg:
                        i2i_p.cfg_scale = lerp_at_t(min_i2i_cfg, max_i2i_cfg, i2i_t)
                    i2i_p.denoising_strength = lerp_at_t(min_denoise_str, max_denoise_str, i2i_t)
                    processed = process_images(i2i_p)

                if not state.interrupted:
                    all_images.append(processed.images[0])
                    if i == 0 and mode_select == "img2img morph":
                        i2i_p.init_images.append(all_images[0])
        if save_video:
            clip = ImageSequenceClip.ImageSequenceClip([np.asarray(t) for t in all_images], fps=video_fps)
            clip.write_videofile(os.path.join(morph_path, f"morph-{morph_number:05}.webm"), codec='libvpx-vp9', ffmpeg_params=['-pix_fmt', 'yuv420p', '-crf', '32', '-b:v', '0'], logger=None)

        prompt = "\n".join([f"{seed} | {prompt}" for seed, prompt in prompts])
        # TODO: instantiate new Processed instead of overwriting one from the loop
        processed.all_prompts = [prompt]
        processed.prompt = prompt
        processed.info = processed.infotext(p, 0)

        processed.images = all_images
        # limit max images shown to avoid lagging out the interface
        if len(processed.images) > 25:
            processed.images = n_evenly_spaced(processed.images, 25)

        if opts.return_grid:
            grid = images.image_grid(processed.images)
            processed.images.insert(0, grid)
            if opts.grid_save:
                images.save_image(grid, p.outpath_grids, "grid", processed.all_seeds[0], processed.prompt, opts.grid_format, info=processed.infotext(p, 0), short_filename=not opts.grid_extended_filename, p=p, grid=True)

        return processed
