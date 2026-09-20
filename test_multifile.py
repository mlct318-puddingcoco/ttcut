"""Virtual-time sources, organized output, and a short real FFmpeg join."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from match_io import (SourceError, natural_paths, make_sources, reorder_sources,
                      source_at, compatibility_issues, geometry_mismatch,
                      output_layout)
from ttcut_v2_3 import (Handler, STATE, STATE_LOCK, HW_ENCODER, IS_MAC,
                        build_render, plan, run_job_managed, probe, probe_audio)


def fake_info(duration, w=320, h=180, fps=30):
    return dict(duration=duration, w=w, h=h, fps=fps, fps_frac=f"{fps}/1",
                codec="h264", pix_fmt="yuv420p", trc="")


class VirtualTimelineTests(unittest.TestCase):
    def sources(self, tmp, durations):
        paths = []
        infos = {}
        for i, duration in enumerate(durations):
            path = str(Path(tmp) / f"DJI_{i+15:04d}.MP4")
            Path(path).touch()
            paths.append(path)
            infos[path] = fake_info(duration)
        return make_sources(paths, lambda p, _: infos[p], lambda p, _: dict(codec_name="aac"))

    def test_two_and_three_files_and_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            two = self.sources(tmp, [1715, 1537])
            self.assertEqual([(s["offset"], s["end"]) for s in two],
                             [(0, 1715), (1715, 3252)])
            self.assertEqual(source_at(two, 0), (0, 0))
            self.assertEqual(source_at(two, 1715), (1, 0))
            self.assertEqual(source_at(two, 1880), (1, 165))
            self.assertEqual(source_at(two, 3252), (1, 1537))
            three = self.sources(tmp, [5, 7, 9])
            self.assertEqual([s["offset"] for s in three], [0, 5, 12])
            self.assertEqual(source_at(three, 12), (2, 0))
            self.assertEqual(source_at(three, 20), (2, 8))
            reordered = reorder_sources(three, [2, 0, 1])
            self.assertEqual([s["duration"] for s in reordered], [9, 5, 7])
            self.assertEqual([s["offset"] for s in reordered], [0, 9, 14])
            self.assertEqual(natural_paths(['DJI_16.MP4', 'DJI_2.MP4']),
                             ['DJI_2.MP4', 'DJI_16.MP4'])

    def test_old_single_file_document_keeps_original_cut_plan(self):
        old_doc = dict(players={'A':'甲','B':'乙'}, events=[
            dict(t=1,type='serve'), dict(t=2,type='point',winner='A')])
        original = plan(old_doc, {})
        with_source_metadata = plan(dict(old_doc, sources=[dict(path='/old.mp4',
            duration=3, offset=0)]), {})
        self.assertTrue(original['ok'])
        self.assertEqual(original['keeps'], with_source_metadata['keeps'])
        self.assertEqual(original['scoring'], with_source_metadata['scoring'])

    def test_single_file_render_persists_authoritative_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / 'DJI_0005_D.MP4')
            Path(source).touch()
            sources = [dict(path=source, offset=0.0, end=3.0,
                            **fake_info(3), audio=None)]
            payload = dict(source='DJI_0005_D.MP4', players={'A':'甲','B':'乙'},
                           firstServer='A', events=[
                               {'t':.2,'type':'serve'},
                               {'t':1.0,'type':'point','winner':'A','highlight':True}])
            handler = Handler.__new__(Handler)
            seen, launched = [], []
            handler._json = lambda obj, code=200: seen.append((code, obj))
            handler._body = lambda: dict(doc=payload, opt={},
                                         out=str(Path(tmp) / 'match.mp4'),
                                         customOutput=True, organization='same-folder')

            class DeferredThread:
                def __init__(self, target=None, args=(), daemon=None):
                    launched.append((target, args))
                def start(self):
                    pass

            with STATE_LOCK:
                old_state = dict(STATE)
                STATE.update(video=source, sources=sources, ffmpeg='ffmpeg',
                             ffprobe='ffprobe', job=None, custom_out=None)
            try:
                with patch('ttcut_v2_3.build_render', return_value=(['ffmpeg'], tmp, None)), \
                     patch('ttcut_v2_3.threading.Thread', DeferredThread):
                    handler._render()
                self.assertEqual(seen[-1][0], 200)
                saved_doc = launched[-1][1][6]
                self.assertEqual(saved_doc['source'], 'DJI_0005_D.MP4')
                self.assertEqual(saved_doc['sources'], [{
                    'path':source, 'duration':3.0, 'offset':0.0, 'end':3.0}])
                launched[-1][1][4].cleanup()
            finally:
                with STATE_LOCK:
                    STATE.update(old_state)

    def test_json_reopen_missing_file_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = self.sources(tmp, [2, 3])
            saved = [{k: s[k] for k in ('path', 'duration', 'offset')} for s in original]
            reopened = make_sources([s['path'] for s in saved],
                                    lambda p, _: fake_info(2 if p.endswith('0015.MP4') else 3),
                                    lambda p, _: dict(codec_name='aac'))
            self.assertEqual([s['offset'] for s in reopened], [0, 2])
            self.assertEqual(source_at(reopened, 2.5), (1, .5))
            handler = Handler.__new__(Handler)
            seen = []
            handler._json = lambda obj, code=200: seen.append((code, obj))
            handler._body = lambda: dict(sources=saved)
            with STATE_LOCK:
                old = dict(STATE)
                STATE['job'] = None
            try:
                with patch('ttcut_v2_3.probe', side_effect=lambda p, _: fake_info(
                     2 if p.endswith('0015.MP4') else 3)), \
                     patch('ttcut_v2_3.probe_audio', return_value=None):
                    handler._load_sources()
                    self.assertEqual(seen[-1][1]['totalDuration'], 5)
                    self.assertEqual([s['offset'] for s in STATE['sources']], [0, 2])
                    Path(original[1]['path']).unlink()
                    handler._load_sources()
                self.assertEqual(seen[-1][0], 400)
                self.assertEqual(seen[-1][1]['error'], '找不到 DJI_0016.MP4')
            finally:
                with STATE_LOCK:
                    STATE.update(old)
            with self.assertRaisesRegex(SourceError, '找不到 DJI_0016.MP4'):
                make_sources([s['path'] for s in saved], lambda *_: fake_info(2), lambda *_: None)
            different = [dict(original[0]), dict(original[1], w=640, fps_frac='24/1')]
            self.assertTrue(geometry_mismatch(different))
            self.assertIn('解析度', compatibility_issues(different))
            self.assertIn('影格率', compatibility_issues(different))

    def test_output_layout_and_duplicate_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = str(Path(tmp) / '許宸愷_vs_曾柏誠.mp4')
            same = output_layout(base, 'same-folder')
            self.assertEqual(same['out'], base)
            self.assertEqual(same['tags'], str(Path(tmp) / '許宸愷_vs_曾柏誠.tags.json'))
            folder = output_layout(base, 'match-folder')
            self.assertEqual(folder['out'], str(Path(tmp) / '許宸愷_vs_曾柏誠' /
                                               '許宸愷_vs_曾柏誠.mp4'))
            self.assertEqual(folder['tags'], str(Path(tmp) / '許宸愷_vs_曾柏誠' /
                                                'ttcut-data' / '許宸愷_vs_曾柏誠.tags.json'))
            Path(folder['folder']).mkdir()
            second = output_layout(base, 'match-folder')
            self.assertTrue(second['out'].endswith('許宸愷_vs_曾柏誠_2/許宸愷_vs_曾柏誠_2.mp4'))
            Path(second['folder']).mkdir()
            third = output_layout(base, 'match-folder')
            self.assertTrue(third['out'].endswith('許宸愷_vs_曾柏誠_3/許宸愷_vs_曾柏誠_3.mp4'))
            self.assertTrue(third['thumbnail'].endswith('_3.thumbnail.jpg'))
            self.assertTrue(third['tags'].endswith('_3.tags.json'))
            self.assertTrue(output_layout(base, 'match-folder', automatic=False)['conflict'])

    def test_rally_offsets_and_new_match_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = self.sources(tmp, [2, 3])
            handler = Handler.__new__(Handler)
            seen = []
            handler._json = lambda obj, code=200: seen.append((code, obj))
            handler._body = lambda: dict(roi=dict(x=0, y=0, w=1, h=1), duration=5)
            with STATE_LOCK:
                old = dict(STATE)
                STATE.update(video=sources[0]['path'], sources=sources, ffmpeg='ffmpeg',
                             job=None, custom_out=str(Path(tmp) / 'custom.mp4'))
            result = dict(candidates=[dict(start=.2, end=.8, visualStart=.3,
                                           visualEnd=.7, splitPoint=.5,
                                           splitChecks=[dict(point=.4)])], diagnostics={})
            try:
                with patch('ttcut_v2_3.detect_video', return_value=result):
                    handler._detect_rallies()
                candidates = seen[-1][1]['candidates']
                self.assertEqual([c['start'] for c in candidates], [.2, 2.2])
                self.assertEqual(candidates[1]['splitChecks'][0]['point'], 2.4)
                handler._body = lambda: dict(separateRois=True, rois=[dict(x=0,y=0,w=1,h=1)])
                handler._detect_rallies()
                self.assertEqual(seen[-1][0], 400)
                self.assertIn('逐段框選 ROI', seen[-1][1]['error'])
                handler._body = lambda: dict(roi=dict(x=0, y=0, w=1, h=1), duration=5)
                with STATE_LOCK:
                    STATE['sources'] = [sources[0], dict(sources[1], w=640)]
                handler._detect_rallies()
                self.assertEqual(seen[-1][0], 400)
                self.assertIn('逐段框選 ROI', seen[-1][1]['error'])
                handler._body = lambda: dict(rois=[dict(x=0,y=0,w=1,h=1),
                                                   dict(x=.1,y=.1,w=.8,h=.8)])
                with patch('ttcut_v2_3.detect_video', return_value=result) as detect:
                    handler._detect_rallies()
                self.assertEqual(seen[-1][0], 200)
                self.assertEqual(detect.call_args_list[1].args[1]['x'], .1)
                handler._new_match()
                self.assertEqual(STATE['sources'], [])
                self.assertIsNone(STATE['custom_out'])
            finally:
                with STATE_LOCK:
                    STATE.update(old)

    def test_save_as_root_and_basename_in_match_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / '北港媽祖盃'
            root.mkdir()
            base = str(root / '許宸愷_vs_曾柏誠.mp4')
            layout = output_layout(base, 'match-folder', automatic=False)
            self.assertEqual(layout['out'], str(root / '許宸愷_vs_曾柏誠' /
                                                '許宸愷_vs_曾柏誠.mp4'))
            self.assertEqual(layout['thumbnail'], str(root / '許宸愷_vs_曾柏誠' /
                                                      '許宸愷_vs_曾柏誠.thumbnail.jpg'))
            self.assertEqual(layout['tags'], str(root / '許宸愷_vs_曾柏誠' /
                                                 'ttcut-data' / '許宸愷_vs_曾柏誠.tags.json'))

    def test_render_places_tags_in_visible_data_folder_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = str(Path(tmp) / 'source.mp4')
            Path(source).touch()
            sources = [dict(path=source, offset=0, end=6, **fake_info(6), audio=None)]
            Path(tmp, 'source.cut').mkdir()
            doc = dict(players={'A':'A','B':'B'}, events=[
                dict(t=1,type='serve'),dict(t=2,type='point',winner='A')])
            handler = Handler.__new__(Handler)
            seen = []
            handler._json = lambda obj, code=200: seen.append((code, obj))
            handler._body = lambda: dict(doc=doc, opt={'intro':{'enabled':False}},
                                         organization='match-folder', customOutput=False)
            with STATE_LOCK:
                old = dict(STATE)
                STATE.update(video=source, sources=sources, ffmpeg='ffmpeg',
                             ffprobe='ffprobe', job=None, custom_out=None)
            try:
                with patch('ttcut_v2_3.build_render', return_value=([], tmp, '')), \
                     patch('ttcut_v2_3.threading.Thread') as thread:
                    handler._render()
                    args = thread.call_args.kwargs['args']
                layout = seen[-1][1]['layout']
                self.assertTrue(layout['out'].endswith('source.cut_2/source.cut_2.mp4'))
                self.assertTrue(layout['tags'].endswith('source.cut_2/ttcut-data/source.cut_2.tags.json'))
                temp_path = args[4].name
                with patch('ttcut_v2_3.run_job', side_effect=lambda job, *_: setattr(job, 'state', 'done')):
                    run_job_managed(*args)
                self.assertTrue(Path(layout['tags']).exists())
                self.assertFalse(Path(temp_path).exists())
            finally:
                with STATE_LOCK:
                    STATE.update(old)


class RealJoinTests(unittest.TestCase):
    def test_two_synthetic_segments_have_video_and_audio_across_join(self):
        ffmpeg = '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
        if not os.path.isfile(ffmpeg):
            ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('FFmpeg unavailable')
        ffprobe = os.path.join(os.path.dirname(ffmpeg), 'ffprobe')
        if not os.path.isfile(ffprobe):
            self.skipTest('FFprobe unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, (color, frequency) in enumerate((('red', 440), ('blue', 880))):
                path = str(Path(tmp) / f'segment{i}.mp4')
                subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-f', 'lavfi',
                                '-i', f'color=c={color}:s=320x180:r=30:d=2',
                                '-f', 'lavfi', '-i', f'sine=frequency={frequency}:sample_rate=48000:duration=2',
                                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                                '-shortest', path], check=True)
                paths.append(path)
            sources = make_sources(paths, probe, probe_audio, ffprobe)
            doc = dict(players={'A':'A','B':'B'}, scoreboard={'style':'koko'},
                       events=[dict(t=.2,type='serve'),dict(t=1.3,type='point',winner='A'),
                               dict(t=2.2,type='serve'),dict(t=3.5,type='point',winner='B')],
                       pads={'lead':.1,'tail':.1})
            opt = dict(quality='high', encoder='libx264', hwaccel='none',
                       intro={'enabled':False})
            planned = plan(doc, opt)
            self.assertTrue(planned['ok'])
            self.assertTrue(planned['keeps'][0][0] < 2 < planned['keeps'][-1][1])
            layout = output_layout(str(Path(tmp) / 'match.mp4'), 'match-folder')
            Path(layout['folder']).mkdir()
            with tempfile.TemporaryDirectory() as workdir:
                cmd, cwd, _ = build_render(doc, planned, paths[0], layout['out'], opt,
                                            ffmpeg, ffprobe, log=lambda _: None,
                                            sources=sources, workdir_override=workdir)
                subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)
                if IS_MAC:
                    accelerated, _, _ = build_render(doc, planned, paths[0], layout['out'],
                        dict(quality='fast', intro={'enabled':False}), ffmpeg, ffprobe,
                        log=lambda _: None, sources=sources, workdir_override=workdir)
                    self.assertIn(HW_ENCODER, accelerated)
                    self.assertEqual(accelerated.count('videotoolbox'), 2)
            self.assertEqual(len(list(Path(layout['folder']).glob('*.mp4'))), 1)
            self.assertEqual(list(Path(layout['folder']).glob('*.ass')), [])
            def pixel(t):
                raw = subprocess.run([ffmpeg, '-v', 'error', '-ss', str(t), '-i', layout['out'],
                                      '-vf', 'crop=1:1:300:150,format=rgb24', '-frames:v', '1',
                                      '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
                return tuple(raw[:3])
            before, after = pixel(1.0), pixel(2.5)
            self.assertGreater(before[0], before[2] + 80)
            self.assertGreater(after[2], after[0] + 80)
            def crossings(t):
                raw = subprocess.run([ffmpeg, '-v', 'error', '-ss', str(t), '-t', '0.2',
                                      '-i', layout['out'], '-ac', '1', '-ar', '8000',
                                      '-f', 's16le', '-'], capture_output=True, check=True).stdout
                import struct
                samples = struct.unpack('<' + 'h' * (len(raw)//2), raw)
                return sum((a < 0) != (b < 0) for a,b in zip(samples,samples[1:]))
            self.assertGreater(crossings(2.5), crossings(1.0) * 1.5)

            intro = dict(enabled=True, duration=.5, thumbnail=True,
                         tournament='Test', category='Final', playerA='A',
                         schoolA='One', playerB='B', schoolB='Two')
            web_doc = dict(doc, intro=intro)
            handler = Handler.__new__(Handler)
            seen = []
            handler._json = lambda obj, code=200: seen.append((code, obj))
            handler._body = lambda: dict(doc=web_doc,
                                         opt=dict(opt, intro=intro),
                                         organization='match-folder',
                                         customOutput=False)
            with STATE_LOCK:
                old_state = dict(STATE)
                STATE.update(video=paths[0], sources=sources, ffmpeg=ffmpeg,
                             ffprobe=ffprobe, job=None, custom_out=None)
            try:
                handler._render()
                self.assertEqual(seen[-1][0], 200)
                deadline = time.monotonic() + 10
                while STATE['job'].state == 'running' and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(STATE['job'].state, 'done', STATE['job'].message)
                organized = seen[-1][1]['layout']
                self.assertEqual({p.name for p in Path(organized['folder']).iterdir()},
                                 {Path(organized['out']).name,
                                  Path(organized['thumbnail']).name, 'ttcut-data'})
                self.assertTrue(Path(organized['tags']).is_file())
                saved = json.loads(Path(organized['tags']).read_text(encoding='utf-8'))
                self.assertEqual(len(saved['sources']), 2)
                self.assertAlmostEqual(saved['sources'][1]['offset'], sources[1]['offset'])
                self.assertEqual(saved['outputOrganization'], 'match-folder')
                self.assertEqual(len(list(Path(organized['folder']).glob('*.mp4'))), 1)
                self.assertTrue(all(Path(p).is_file() for p in paths))
            finally:
                with STATE_LOCK:
                    STATE.update(old_state)

    def test_incompatible_resolution_fps_and_audio_are_normalized(self):
        ffmpeg = '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
        ffprobe = '/opt/homebrew/opt/ffmpeg-full/bin/ffprobe'
        if not os.path.isfile(ffmpeg) or not os.path.isfile(ffprobe):
            self.skipTest('FFmpeg full unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, (size, fps, rate) in enumerate((("320x180",30,48000),
                                                   ("640x360",24,44100))):
                path = str(Path(tmp) / f'segment{i}.mp4')
                subprocess.run([ffmpeg,'-y','-loglevel','error','-f','lavfi','-i',
                                f'color=c=blue:s={size}:r={fps}:d=2',
                                '-f','lavfi','-i',f'sine=frequency=440:sample_rate={rate}:duration=2',
                                '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',
                                '-shortest',path],check=True)
                paths.append(path)
            sources = make_sources(paths, probe, probe_audio, ffprobe)
            issues = compatibility_issues(sources)
            self.assertIn('解析度', issues)
            self.assertIn('影格率', issues)
            self.assertIn('音訊規格', issues)
            doc = dict(players={'A':'A','B':'B'},events=[
                dict(t=.2,type='serve'),dict(t=1.2,type='point',winner='A'),
                dict(t=2.2,type='serve'),dict(t=3.5,type='point',winner='B')],
                pads={'lead':.1,'tail':.1})
            opt = dict(quality='high',encoder='libx264',hwaccel='none',
                       intro={'enabled':False})
            out = str(Path(tmp) / 'normalized.mp4')
            with tempfile.TemporaryDirectory() as workdir:
                cmd,cwd,_ = build_render(doc,plan(doc,opt),paths[0],out,opt,ffmpeg,ffprobe,
                                         log=lambda _:None,sources=sources,
                                         workdir_override=workdir)
                subprocess.run(cmd,cwd=cwd,check=True,capture_output=True)
            info = probe(out,ffprobe)
            self.assertEqual((info['w'],info['h']), (320,180))
            self.assertGreater(info['duration'], 3.4)


if __name__ == '__main__':
    unittest.main()
