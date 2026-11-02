"""Element library checks: shape functions, integration, orderings."""
import numpy as np
import pytest

from opencae.meshing import elements as E

TET_NAT = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
HEX_NAT = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                    [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], float)


def natural_nodes(et):
    base = TET_NAT if et.shape == "tet" else HEX_NAT
    if et.order == 1:
        return base
    mids = [(base[i] + base[j]) / 2 for i, j in et.edges]
    return np.vstack([base, mids])


@pytest.mark.parametrize("name", ["C3D4", "C3D10", "C3D8", "C3D20"])
def test_solid_shape_functions(name):
    et = E.SOLID_TYPES[name]
    R = natural_nodes(et)
    assert len(R) == et.n_nodes
    for i, r in enumerate(R):  # Kronecker delta
        N, _ = E.solid_shape(et, r)
        assert np.allclose(N, np.eye(et.n_nodes)[i], atol=1e-12)
    rng = np.random.default_rng(0)
    for _ in range(5):
        r = rng.uniform(0.05, 0.3, 3) if et.shape == "tet" else rng.uniform(-0.9, 0.9, 3)
        N, dN = E.solid_shape(et, r)
        assert N.sum() == pytest.approx(1.0) and np.allclose(dN.sum(axis=0), 0, atol=1e-12)
        for d in range(3):  # derivatives vs central differences
            h = 1e-6
            rp, rm = r.copy(), r.copy()
            rp[d] += h
            rm[d] -= h
            fd = (E.solid_shape(et, rp)[0] - E.solid_shape(et, rm)[0]) / (2 * h)
            assert np.allclose(fd, dN[:, d], atol=1e-7)


@pytest.mark.parametrize("nn", [3, 6, 4, 8])
def test_surface_shape_functions_and_area(nn):
    tri = nn in (3, 6)
    base = np.array([[0, 0], [1, 0], [0, 1]], float) if tri else np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], float)
    nc = len(base)
    nat = base if nn == nc else np.vstack([base, [(base[i] + base[(i + 1) % nc]) / 2 for i in range(nc)]])
    for i, (a, b) in enumerate(nat):
        N, _ = E.surface_shape(nn, a, b)
        assert np.allclose(N, np.eye(nn)[i], atol=1e-12)
    # physical face: map natural coordinates to a scaled plane, check area and load weights sum to area
    X = np.column_stack([3 * nat[:, 0], 2 * nat[:, 1], np.zeros(nn)])[None]
    wN, area, nvec = E.surface_integrals(X[0], np.arange(nn)[None])
    exact = 3 * 2 * (0.5 if tri else 4)
    assert area[0] == pytest.approx(exact) and wN.sum() == pytest.approx(exact)
    assert abs(nvec[0, 2]) == pytest.approx(exact)
    if nn == 8:  # serendipity: corners -1/12, mids +1/3 of area
        assert np.allclose(wN[0, :4], -exact / 12) and np.allclose(wN[0, 4:], exact / 3)
    if nn == 6:
        assert np.allclose(wN[0, :3], 0, atol=1e-12) and np.allclose(wN[0, 3:], exact / 3)


@pytest.mark.parametrize("name", ["C3D4", "C3D10", "C3D8", "C3D20"])
def test_volume_integration(name):
    et = E.SOLID_TYPES[name]
    X = natural_nodes(et) * np.array([2.0, 3.0, 5.0])  # affine stretch
    w, vol = E.nodal_volume_weights(X, np.arange(et.n_nodes)[None], name)
    exact = (1 / 6 if et.shape == "tet" else 8) * 30
    assert vol == pytest.approx(exact) and w.sum() == pytest.approx(exact)
    assert E.element_centroid_jacobian_sign(X, np.arange(et.n_nodes)[None], name)[0] == 1


def test_hex_faces_outward_complete():
    et = E.SOLID_TYPES["C3D20"]
    # each corner appears in exactly 3 faces, each edge's mid node in exactly 2 face_mids
    cnt = np.bincount(np.array(et.faces).ravel(), minlength=8)
    assert (cnt == 3).all()
    mids = np.bincount(np.array(et.face_mids).ravel(), minlength=20)[8:]
    assert (mids == 2).all()
