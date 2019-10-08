#
# Copyright (C) 2019  Leo Singer
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
"""Rough-cut injection tool.

The idea is to efficiently sample events, uniformly in comoving volume, and
from a distribution of masses and spins, such that later detection cuts will
not reject an excessive number of events. We divide the intrinsic parameter
space into a very coarse grid and we calculate the maximum horizon distance in
each grid cell."""

import functools

from astropy import cosmology
from astropy.cosmology import LambdaCDM
from astropy.cosmology.core import vectorize_if_needed
from astropy import units
from astropy.units import dimensionless_unscaled
import lal
import numpy as np
from scipy.integrate import quad, fixed_quad
from scipy.interpolate import interp1d
from scipy.optimize import root_scalar

from ..bayestar.filter import sngl_inspiral_psd
from . import (
    ArgumentParser, FileType, random_parser, register_to_xmldoc, write_fileobj)

lal.ClobberDebugLevel(lal.LALNDEBUG)


def get_decisive_snr(snrs):
    """Return the SNR for the trigger that decides if an event is detectable.

    If there are two or more detectors, then the decisive SNR is the SNR of the
    second loudest detector (since a coincidence of two or more events is
    required). If there is only one detector, then the decisive SNR is just the
    SNR of that detector. If there are no detectors, then 0 is returned.

    Parameters
    ----------
    snrs : list
        List of SNRs (floats).

    Returns
    -------
    decisive_snr : float
    """
    if len(snrs) > 1:
        return sorted(snrs)[-2]
    elif len(snrs) == 1:
        return snrs[0]
    else:
        return 0.0


def lo_hi_nonzero(x):
    nonzero = np.flatnonzero(x)
    return nonzero[0], nonzero[-1]


def z_at_snr(cosmo, psds, waveform, f_low, snr, params):
    """
    Get redshift at which a waveform attains a given SNR.

    Parameters
    ----------
    cosmo : :class:`astropy.cosmology.FLRW`
        The cosmological model.
    psds : list
        List of :class:`lal.REAL8FrequencySeries` objects.
    waveform : str
        Waveform approximant name.
    f_low : float
        Low-frequency cutoff for template.
    snr : float
        Target SNR.
    params : list
        List of waveform parameters: mass1, mass2, spin1z, spin2z.

    Returns
    -------
    comoving_distance : float
        Comoving distance in Mpc.
    """
    # Construct waveform
    mass1, mass2, spin1z, spin2z = params
    series = sngl_inspiral_psd(waveform, f_low=f_low,
                               mass1=mass1, mass2=mass2,
                               spin1z=spin1z, spin2z=spin2z)
    i_lo, i_hi = lo_hi_nonzero(series.data.data)
    log_f = np.log(series.f0 + series.deltaF * np.arange(i_lo, i_hi + 1))
    log_f_lo = log_f[0]
    log_f_hi = log_f[-1]
    num = interp1d(
        log_f, np.log(series.data.data[i_lo:i_hi + 1]),
        fill_value=-np.inf, bounds_error=False, assume_sorted=True)

    denoms = []
    for series in psds:
        i_lo, i_hi = lo_hi_nonzero(
            np.isfinite(series.data.data) & (series.data.data != 0))
        log_f = np.log(series.f0 + series.deltaF * np.arange(i_lo, i_hi + 1))
        denom = interp1d(
            log_f, log_f - np.log(series.data.data[i_lo:i_hi + 1]),
            fill_value=-np.inf, bounds_error=False, assume_sorted=True)
        denoms.append(denom)

    def snr_at_z(z):
        logzp1 = np.log(z + 1)
        integrand = lambda log_f: [
            np.exp(num(log_f + logzp1) + denom(log_f)) for denom in denoms]
        integrals, _ = fixed_quad(
            integrand, log_f_lo, log_f_hi - logzp1, n=1024)
        snr = get_decisive_snr(np.sqrt(4 * integrals))
        with np.errstate(divide='ignore'):
            snr /= cosmo.angular_diameter_distance(z).to_value(units.Mpc)
        return snr

    return root_scalar(lambda z: snr_at_z(z) - snr, bracket=(0, 1e3)).root


def z_at_comoving_distance(cosmo, d):
    """Get the redshift as a function of comoving distance.

    Parameters
    ----------
    cosmo : :class:`astropy.cosmology.LambdaCDM`
        The cosmological model.
    d : :class:`astropy.units.Quantity`
        The distance in Mpc (may be scalar or a Numpy array).

    Returns
    -------
    z : float, :class:`numpy.ndarray`
        The redshift.

    Notes
    -----
    This function is optimized for ΛCDM cosmologies. For more general
    cosmological models, use :func:`astropy.cosmology.z_at_value`.

    The optimization consists of passing Scipy's root finder a bracketing
    interval that is guaranteed to contain the solution. This enables
    convergence across (nearly) all physically valid values of the comoving
    distance without the need to tune `z_min` and `z_max`.
    """
    if not isinstance(cosmo, LambdaCDM):
        raise NotImplementedError(
            'This method is optimized for LambdaCDM cosmologies. For more '
            'general cosmologies, use astropy.cosmology.z_at_value.')

    inv_efunc_scalar_args = cosmo._inv_efunc_scalar_args
    r = (d / cosmo.hubble_distance).to_value(
        dimensionless_unscaled)
    r_max = (cosmo.comoving_distance(np.inf) / cosmo.hubble_distance).to_value(
        dimensionless_unscaled)
    fprime = lambda z: cosmo._inv_efunc_scalar(z, *inv_efunc_scalar_args)
    eps = np.finfo(np.float).eps

    def z_at_r(r):
        if r > r_max:
            return np.nan
        f = lambda z: quad(fprime, 0, z)[0] - r
        z_max = r / np.square(1 - np.sqrt(r / r_max))
        xtol = max(2e-12 * z_max, eps)
        return root_scalar(f, bracket=[r, z_max], xtol=xtol).root

    return vectorize_if_needed(z_at_r, r)


def assert_not_reached():  # pragma: no cover
    raise AssertionError('This line should not be reached.')


def parser():
    parser = ArgumentParser(parents=[random_parser])
    parser.add_argument(
        '--cosmology', choices=cosmology.parameters.available,
        default='WMAP9', help='Cosmological model')
    parser.add_argument(
        '--distribution', required=True, choices=(
            'bns_astro', 'bns_broad', 'nsbh_astro', 'nsbh_broad',
            'bbh_astro', 'bbh_broad'))
    parser.add_argument('--reference-psd', type=FileType('rb'), required=True)
    parser.add_argument('--f-low', type=float, default=25.0)
    parser.add_argument('--min-snr', type=float, default=4)
    parser.add_argument('--waveform', default='o2-uberbank')
    parser.add_argument('--nsamples', type=int, default=100000)
    parser.add_argument('-o', '--output', type=FileType('wb'), default='-')
    return parser


def main(args=None):
    import itertools

    from glue.ligolw import lsctables
    from glue.ligolw.utils import process as ligolw_process
    from glue.ligolw import utils as ligolw_utils
    from glue.ligolw import ligolw
    import lal.series
    from scipy import stats

    from ..util import progress_map

    p = parser()
    args = p.parse_args(args)

    xmldoc = ligolw.Document()
    xmlroot = xmldoc.appendChild(ligolw.LIGO_LW())
    process = register_to_xmldoc(xmldoc, p, args)

    cosmo = cosmology.default_cosmology.get_cosmology_from_string(
        args.cosmology)

    ns_mass_min = 1.0
    ns_mass_max = 2.0
    bh_mass_min = 5.0
    bh_mass_max = 50.0

    ns_astro_spin_min = -0.05
    ns_astro_spin_max = +0.05
    ns_astro_mass_dist = stats.norm(1.33, 0.09)
    ns_astro_spin_dist = stats.uniform(
        ns_astro_spin_min, ns_astro_spin_max - ns_astro_spin_min)

    ns_broad_spin_min = -0.4
    ns_broad_spin_max = +0.4
    ns_broad_mass_dist = stats.uniform(ns_mass_min, ns_mass_max - ns_mass_min)
    ns_broad_spin_dist = stats.uniform(
        ns_broad_spin_min, ns_broad_spin_max - ns_broad_spin_min)

    bh_astro_spin_min = -0.99
    bh_astro_spin_max = +0.99
    bh_astro_mass_dist = stats.pareto(b=1.3)
    bh_astro_spin_dist = stats.uniform(
        bh_astro_spin_min, bh_astro_spin_max - bh_astro_spin_min)

    bh_broad_spin_min = -0.99
    bh_broad_spin_max = +0.99
    bh_broad_mass_dist = stats.reciprocal(bh_mass_min, bh_mass_max)
    bh_broad_spin_dist = stats.uniform(
        bh_broad_spin_min, bh_broad_spin_max - bh_broad_spin_min)

    if args.distribution.startswith('bns_'):
        m1_min = m2_min = ns_mass_min
        m1_max = m2_max = ns_mass_max
        if args.distribution.endswith('_astro'):
            x1_min = x2_min = ns_astro_spin_min
            x1_max = x2_max = ns_astro_spin_max
            m1_dist = m2_dist = ns_astro_mass_dist
            x1_dist = x2_dist = ns_astro_spin_dist
        elif args.distribution.endswith('_broad'):
            x1_min = x2_min = ns_broad_spin_min
            x1_max = x2_max = ns_broad_spin_max
            m1_dist = m2_dist = ns_broad_mass_dist
            x1_dist = x2_dist = ns_broad_spin_dist
        else:  # pragma: no cover
            assert_not_reached()
    elif args.distribution.startswith('nsbh_'):
        m1_min = bh_mass_min
        m1_max = bh_mass_max
        m2_min = ns_mass_min
        m2_max = ns_mass_max
        if args.distribution.endswith('_astro'):
            x1_min = bh_astro_spin_min
            x1_max = bh_astro_spin_max
            x2_min = ns_astro_spin_min
            x2_max = ns_astro_spin_max
            m1_dist = bh_astro_mass_dist
            m2_dist = ns_astro_mass_dist
            x1_dist = bh_astro_spin_dist
            x2_dist = ns_astro_spin_dist
        elif args.distribution.endswith('_broad'):
            x1_min = bh_broad_spin_min
            x1_max = bh_broad_spin_max
            x2_min = ns_broad_spin_min
            x2_max = ns_broad_spin_max
            m1_dist = bh_broad_mass_dist
            m2_dist = ns_broad_mass_dist
            x1_dist = bh_broad_spin_dist
            x2_dist = ns_broad_spin_dist
        else:  # pragma: no cover
            assert_not_reached()
    elif args.distribution.startswith('bbh_'):
        m1_min = m2_min = bh_mass_min
        m1_max = m2_max = bh_mass_max
        if args.distribution.endswith('_astro'):
            x1_min = x2_min = bh_astro_spin_min
            x1_max = x2_max = bh_astro_spin_max
            m1_dist = m2_dist = bh_astro_mass_dist
            x1_dist = x2_dist = bh_astro_spin_dist
        elif args.distribution.endswith('_broad'):
            x1_min = x2_min = bh_broad_spin_min
            x1_max = x2_max = bh_broad_spin_max
            m1_dist = m2_dist = bh_broad_mass_dist
            x1_dist = x2_dist = bh_broad_spin_dist
        else:  # pragma: no cover
            assert_not_reached()
    else:  # pragma: no cover
        assert_not_reached()

    dists = (m1_dist, m2_dist, x1_dist, x2_dist)

    # Read PSDs
    psds = list(
        lal.series.read_psd_xmldoc(
            ligolw_utils.load_fileobj(
                args.reference_psd,
                contenthandler=lal.series.PSDContentHandler)[0]).values())

    # Construct mass1, mass2, spin1z, spin2z grid.
    m1 = np.geomspace(m1_min, m1_max, 5)
    m2 = np.geomspace(m2_min, m2_max, 5)
    x1 = np.linspace(x1_min, x1_max, 5)
    x2 = np.linspace(x2_min, x2_max, 5)
    params = m1, m2, x1, x2

    # Calculate the maximum distance on the grid.
    shape = tuple(len(param) for param in params)
    max_z = np.reshape(
        progress_map(
            functools.partial(
                z_at_snr, cosmo, psds,
                args.waveform, args.f_low, args.min_snr),
            np.column_stack([param.ravel() for param
                             in np.meshgrid(*params, indexing='ij')]),
            multiprocess=True),
        shape)
    max_distance = cosmo.comoving_distance(max_z).to_value(units.Mpc)

    # Make sure that we filled in all entries
    assert np.all(max_distance >= 0)

    # Find piecewise constant approximate upper bound on distance:
    # Calculate approximate gradient at each grid point, then approximate
    # function with a plane at that point, and find the maximum of that plane
    # in a square patch around that point
    max_distance_grad = np.asarray(np.gradient(max_distance, *params))
    param_edges = [
        np.concatenate(((p[0],), 0.5 * (p[1:] + p[:-1]), (p[-1],)))
        for p in params]
    param_los = [param_edge[:-1] for param_edge in param_edges]
    param_his = [param_edge[1:] for param_edge in param_edges]
    lo_hi_deltas = [((param_lo, param_hi) - param)
                    for param_lo, param_hi, param
                    in zip(param_los, param_his, params)]
    corner_deltas = np.asarray([np.meshgrid(*delta, indexing='ij')
                                for delta in itertools.product(*lo_hi_deltas)])
    max_distance += (corner_deltas * max_distance_grad).sum(1).max(0)

    # Truncate maximum distance at the particle horizon.
    max_distance = np.minimum(
        max_distance, cosmo.comoving_distance(np.inf).value)

    # Calculate V * T in each grid cell
    cdf_los = [dist.cdf(param_lo) for param_lo, dist in zip(param_los, dists)]
    cdf_his = [dist.cdf(param_hi) for param_hi, dist in zip(param_his, dists)]
    cdfs = [cdf_hi - cdf_lo for cdf_lo, cdf_hi in zip(cdf_los, cdf_his)]
    probs = np.prod(np.meshgrid(*cdfs, indexing='ij'), axis=0)
    probs /= probs.sum()
    probs *= 4/3*np.pi*max_distance**3
    volume = probs.sum()
    probs /= volume
    probs = probs.ravel()

    volumetric_rate = args.nsamples / volume * units.year**-1 * units.Mpc**-3

    # Draw random grid cells
    dist = stats.rv_discrete(values=(np.arange(len(probs)), probs))
    indices = np.unravel_index(dist.rvs(size=args.nsamples), shape)

    # Draw random intrinsic params from each cell
    values = [
        dist.ppf(stats.uniform(cdf_lo[i], cdf[i]).rvs(size=args.nsamples))
        for i, dist, cdf_lo, cdf in zip(indices, dists, cdf_los, cdfs)]

    # Draw random extrinsic parameters for each cell
    dist = stats.powerlaw(a=3, scale=max_distance[indices])
    values.append(dist.rvs(size=args.nsamples))
    dist = stats.uniform(0, 2 * np.pi)
    values.append(dist.rvs(size=args.nsamples))
    dist = stats.uniform(-1, 2)
    values.append(np.arcsin(dist.rvs(size=args.nsamples)))
    dist = stats.uniform(-1, 2)
    values.append(np.arccos(dist.rvs(size=args.nsamples)))
    dist = stats.uniform(0, 2 * np.pi)
    values.append(dist.rvs(size=args.nsamples))
    dist = stats.uniform(-np.pi, 2 * np.pi)
    values.append(dist.rvs(size=args.nsamples))
    dist = stats.uniform(1e9, units.year.to(units.second))
    values.append(np.sort(dist.rvs(size=args.nsamples)))

    # Populate sim_inspiral table
    sims = xmlroot.appendChild(lsctables.New(lsctables.SimInspiralTable))
    keys = ('mass1', 'mass2', 'spin1z', 'spin2z',
            'distance', 'longitude', 'latitude',
            'inclination', 'polarization', 'coa_phase', 'time_geocent')
    for row in zip(*values):
        sims.appendRow(
            **dict(
                dict.fromkeys(sims.validcolumns, None),
                process_id=process.process_id,
                simulation_id=sims.get_next_id(),
                waveform=args.waveform,
                f_lower=args.f_low,
                **dict(zip(keys, row))))

    # Apply redshift factor
    colnames = ['distance', 'mass1', 'mass2']
    columns = [sims.getColumnByName(colname) for colname in colnames]
    zp1 = 1 + z_at_comoving_distance(cosmo, np.asarray(columns[0]) * units.Mpc)
    for column in columns:
        column[:] = np.asarray(column) * zp1

    # Record process end time.
    process.comment = str(volumetric_rate)
    ligolw_process.set_process_end_time(process)

    # Write output file.
    write_fileobj(xmldoc, args.output)
