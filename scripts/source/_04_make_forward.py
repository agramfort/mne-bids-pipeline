"""Forward solution.

Calculate forward solution for M/EEG channels.
"""

import itertools
import logging
from typing import Optional
from types import SimpleNamespace

import mne
from mne.coreg import Coregistration
from mne_bids import BIDSPath, get_head_mri_trans

import config
from config import gen_log_kwargs, failsafe_run, _get_bem_conductivity
from config import parallel_func, _meg_in_ch_types

logger = logging.getLogger('mne-bids-pipeline')


def _prepare_trans_template(cfg, info):
    assert isinstance(cfg.use_template_mri, str)
    assert cfg.use_template_mri == cfg.fs_subject

    if cfg.fs_subject != "fsaverage" and not cfg.adjust_coreg:
        raise ValueError("Adjusting the coregistration is mandatory "
                         "when using a template MRI different from "
                         "fsaverage")
    if cfg.fs_subject == "fsaverage" and not cfg.adjust_coreg:
        trans = cfg.fs_subjects_dir / cfg.fs_subject / \
            'bem' / f'{cfg.fs_subject}-trans.fif'
    else:
        fiducials = "estimated"  # get fiducials from fsaverage
        coreg = Coregistration(info, cfg.fs_subject, cfg.fs_subjects_dir,
                               fiducials=fiducials)
        coreg.fit_fiducials(verbose=True)
        trans = coreg.trans

    return trans


def _prepare_trans(cfg, bids_path):
    # Generate a head ↔ MRI transformation matrix from the
    # electrophysiological and MRI sidecar files, and save it to an MNE
    # "trans" file in the derivatives folder.
    subject, session = bids_path.subject, bids_path.session

    if config.mri_t1_path_generator is None:
        t1_bids_path = None
    else:
        t1_bids_path = BIDSPath(subject=subject,
                                session=session,
                                root=cfg.bids_root)
        t1_bids_path = config.mri_t1_path_generator(t1_bids_path.copy())
        if t1_bids_path.suffix is None:
            t1_bids_path.update(suffix='T1w')
        if t1_bids_path.datatype is None:
            t1_bids_path.update(datatype='anat')

    if config.mri_landmarks_kind is None:
        landmarks_kind = None
    else:
        landmarks_kind = config.mri_landmarks_kind(
            BIDSPath(subject=subject, session=session)
        )

    msg = 'Estimating head ↔ MRI transform'
    logger.info(**gen_log_kwargs(message=msg, subject=subject,
                                 session=session))

    trans = get_head_mri_trans(
        bids_path.copy().update(run=cfg.runs[0],
                                root=cfg.bids_root,
                                extension=None),
        t1_bids_path=t1_bids_path,
        fs_subject=cfg.fs_subject,
        fs_subjects_dir=cfg.fs_subjects_dir,
        kind=landmarks_kind)

    return trans


def get_input_fnames_forward(*, cfg, subject, session):
    bids_path = BIDSPath(subject=subject,
                         session=session,
                         task=cfg.task,
                         acquisition=cfg.acq,
                         run=None,
                         recording=cfg.rec,
                         space=cfg.space,
                         extension='.fif',
                         datatype=cfg.datatype,
                         root=cfg.deriv_root,
                         check=False)
    in_files = dict()
    in_files['info'] = bids_path.copy().update(**cfg.source_info_path_update)
    bem_path = cfg.fs_subjects_dir / cfg.fs_subject / 'bem'
    _, tag = _get_bem_conductivity(cfg)
    in_files['bem'] = bem_path / f'{cfg.fs_subject}-{tag}-bem-sol.fif'
    in_files['src'] = bem_path / f'{cfg.fs_subject}-{cfg.spacing}-src.fif'
    return in_files


@failsafe_run(script_path=__file__,
              get_input_fnames=get_input_fnames_forward)
def run_forward(*, cfg, subject, session, in_files):
    bids_path = BIDSPath(subject=subject,
                         session=session,
                         task=cfg.task,
                         acquisition=cfg.acq,
                         run=None,
                         recording=cfg.rec,
                         space=cfg.space,
                         extension='.fif',
                         datatype=cfg.datatype,
                         root=cfg.deriv_root,
                         check=False)

    # Info
    info = mne.io.read_info(in_files.pop('info'))
    info = mne.pick_info(
        info,
        mne.pick_types(
            info,
            meg=_meg_in_ch_types(cfg.ch_types),
            eeg="eeg" in cfg.ch_types,
            exclude=[]
        )
    )

    # BEM
    bem = in_files.pop('bem')

    # source space
    src = in_files.pop('src')

    # trans
    if cfg.use_template_mri is not None:
        trans = _prepare_trans_template(cfg, info)
    else:
        trans = _prepare_trans(cfg, bids_path)

    msg = 'Calculating forward solution'
    logger.info(**gen_log_kwargs(message=msg, subject=subject,
                                 session=session))
    fwd = mne.make_forward_solution(info, trans=trans, src=src,
                                    bem=bem, mindist=cfg.mindist)
    out_files = dict()
    out_files['trans'] = bids_path.copy().update(suffix='trans')
    out_files['forward'] = bids_path.copy().update(suffix='fwd')
    mne.write_trans(out_files['trans'], fwd['mri_head_t'], overwrite=True)
    mne.write_forward_solution(out_files['forward'], fwd, overwrite=True)
    return out_files


def get_config(
    subject: Optional[str] = None,
    session: Optional[str] = None
) -> SimpleNamespace:
    cfg = SimpleNamespace(
        task=config.get_task(),
        runs=config.get_runs(subject=subject),
        datatype=config.get_datatype(),
        acq=config.acq,
        rec=config.rec,
        space=config.space,
        mindist=config.mindist,
        spacing=config.spacing,
        use_template_mri=config.use_template_mri,
        adjust_coreg=config.adjust_coreg,
        source_info_path_update=config.source_info_path_update,
        ch_types=config.ch_types,
        fs_subject=config.get_fs_subject(subject=subject),
        fs_subjects_dir=config.get_fs_subjects_dir(),
        deriv_root=config.get_deriv_root(),
        bids_root=config.get_bids_root()
    )
    return cfg


def main():
    """Run forward."""
    if not config.run_source_estimation:
        msg = 'Skipping, run_source_estimation is set to False …'
        logger.info(**gen_log_kwargs(message=msg, emoji='skip'))
        return

    with config.get_parallel_backend():
        parallel, run_func = parallel_func(run_forward)
        logs = parallel(
            run_func(
                cfg=get_config(subject=subject), subject=subject,
                session=session
            )
            for subject, session in
            itertools.product(
                config.get_subjects(),
                config.get_sessions()
            )
        )

        config.save_logs(logs)


if __name__ == '__main__':
    main()
