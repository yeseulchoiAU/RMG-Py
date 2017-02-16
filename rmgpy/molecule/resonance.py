#!/usr/bin/env python
# encoding: utf-8

################################################################################
#
#   RMG - Reaction Mechanism Generator
#
#   Copyright (c) 2009-2011 by the RMG Team (rmg_dev@mit.edu)
#
#   Permission is hereby granted, free of charge, to any person obtaining a
#   copy of this software and associated documentation files (the 'Software'),
#   to deal in the Software without restriction, including without limitation
#   the rights to use, copy, modify, merge, publish, distribute, sublicense,
#   and/or sell copies of the Software, and to permit persons to whom the
#   Software is furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED 'AS IS', WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#   FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#   DEALINGS IN THE SOFTWARE.
#
################################################################################

"""
This module contains methods for generation of resonance structures of molecules.

The main function to generate all relevant resonance structures for a given
Molecule object is ``generateResonanceStructures``. It calls the necessary
functions for generating each type of resonance structure.

Currently supported resonance types:
- All species:
    - ``generateAdjacentResonanceStructures``: single radical shift with double or triple bond
    - ``generateLonePairRadicalResonanceStructures``: single radical shift with lone pair
    - ``generateN5dd_N5tsResonanceStructures``: shift between nitrogen with two double bonds and single + triple bond
- Aromatic species only:
    - ``generateAromaticResonanceStructures``: fully delocalized structure, where all aromatic rings have benzene bonds
    - ``generateKekuleStructure``: generate a single Kekule structure for an aromatic compound (single/double bond form)
    - ``generateOppositeKekuleStructure``: for monocyclic aromatic species, rotate the double bond assignment
    - ``generateClarStructures``: generate all structures with the maximum number of pi-sextet assignments
"""

import cython
import logging
import itertools

from .graph import Vertex, Edge, Graph, getVertexConnectivityValue
from .molecule import Atom, Bond, Molecule
from .kekulize import kekulize
from .atomtype import AtomTypeError
import rmgpy.molecule.pathfinder as pathfinder


def populateResonanceAlgorithms(features=None):
    """
    Generate list of resonance structure algorithms relevant to the current molecule.

    Takes a dictionary of features generated by analyzeMolecule().
    Returns a list of resonance algorithms.
    """
    cython.declare(methodList=list)
    methodList = []

    if features is None:
        methodList = [
            generateAdjacentResonanceStructures,
            generateLonePairRadicalResonanceStructures,
            generateN5dd_N5tsResonanceStructures,
            generateAromaticResonanceStructures,
            generateKekuleStructure,
            generateOppositeKekuleStructure,
            generateClarStructures,
        ]
    else:
        # If the molecule is aromatic, then radical resonance has already been considered
        # If the molecule was falsely identified as aromatic, then isArylRadical will still accurately capture
        # cases where the radical is in an orbital that is orthogonal to the pi orbitals.
        if features['isRadical'] and not features['isAromatic'] and not features['isArylRadical']:
            methodList.append(generateAdjacentResonanceStructures)
        if features['hasNitrogen']:
            methodList.append(generateN5dd_N5tsResonanceStructures)
        if features['hasLonePairs']:
            methodList.append(generateLonePairRadicalResonanceStructures)

    return methodList

def analyzeMolecule(mol):
    """
    Identify key features of molecule important for resonance structure generation.

    Returns a dictionary of features.
    """
    cython.declare(features=dict)

    features = {'isRadical': mol.isRadical(),
                'isCyclic': mol.isCyclic(),
                'isAromatic': False,
                'isPolycyclicAromatic': False,
                'isArylRadical': False,
                'hasNitrogen': False,
                'hasOxygen': False,
                'hasLonePairs': False,
                }

    if features['isCyclic']:
        aromaticRings = mol.getAromaticRings()[0]
        if len(aromaticRings) > 0:
            features['isAromatic'] = True
        if len(aromaticRings) > 1:
            features['isPolycyclicAromatic'] = True
        if features['isRadical'] and features['isAromatic']:
            features['isArylRadical'] = mol.isArylRadical(aromaticRings)
    for atom in mol.vertices:
        if atom.isNitrogen():
            features['hasNitrogen'] = True
        if atom.isOxygen():
            features['hasOxygen'] = True
        if atom.lonePairs > 0:
            features['hasLonePairs'] = True

    return features

def generateResonanceStructures(mol, clarStructures=True, keepIsomorphic=False):
    """
    Generate and return all of the resonance structures for the input molecule.

    Most of the complexity of this method goes into handling aromatic species, particularly to generate an accurate
    set of resonance structures that is consistent regardless of the input structure. The following considerations
    are made:

    1. False positives from RDKit aromaticity detection can occur if a molecule has exocyclic double bonds
    2. False negatives from RDKit aromaticity detection can occur if a radical is delocalized into an aromatic ring
    3. sp2 hybridized radicals in the plane of an aromatic ring do not participate in hyperconjugation
    4. Non-aromatic resonance structures of PAHs are not important resonance contributors (assumption)

    Aromatic species are broken into the following categories for resonance treatment:

    - Radical polycyclic aromatic species: Kekule structures are generated in order to generate adjacent resonance
    structures. The resulting structures are then used for Clar structure generation. After all three steps, any
    non-aromatic structures are removed, under the assumption that they are not important resonance contributors.
    - Radical monocyclic aromatic species: Kekule structures are generated along with adjacent resonance structures.
    All are kept regardless of aromaticity because the radical is more likely to delocalize into the ring.
    - Stable polycyclic aromatic species: Clar structures are generated
    - Stable monocyclic aromatic species: Kekule structures are generated
    """
    cython.declare(molList=list, newMolList=list, features=dict, methodList=list)

    molList = [mol]

    # Analyze molecule
    features = analyzeMolecule(mol)

    # Use generateAromaticResonanceStructures to check for false positives and negatives
    if features['isAromatic'] or (features['isCyclic'] and features['isRadical'] and not features['isArylRadical']):
        newMolList = generateAromaticResonanceStructures(mol, features)
        if len(newMolList) == 0:
            # Encountered false positive, ie. the molecule is not actually aromatic
            features['isAromatic'] = False
            features['isPolycyclicAromatic'] = False
    else:
        newMolList = []

    # Special handling for aromatic species
    if len(newMolList) > 0:
        if features['isRadical'] and not features['isArylRadical']:
            if features['isPolycyclicAromatic']:
                if clarStructures:
                    _generateResonanceStructures(newMolList, [generateKekuleStructure], keepIsomorphic)
                    _generateResonanceStructures(newMolList, [generateAdjacentResonanceStructures], keepIsomorphic)
                    _generateResonanceStructures(newMolList, [generateClarStructures], keepIsomorphic)
                    # Remove non-aromatic structures under the assumption that they aren't important resonance contributors
                    newMolList = [m for m in newMolList if m.isAromatic()]
                else:
                    pass
            else:
                i = len(newMolList)
                _generateResonanceStructures(newMolList, [generateKekuleStructure]), keepIsomorphic
                j = len(newMolList)
                _generateResonanceStructures(newMolList, [generateAdjacentResonanceStructures], keepIsomorphic)
                # Remove the kekule structures that do not have the radical delocalized into the ring
                del newMolList[i:j]
        elif features['isPolycyclicAromatic']:
            if clarStructures:
                _generateResonanceStructures(newMolList, [generateClarStructures], keepIsomorphic)
            else:
                pass
        else:
            # The molecule is an aryl radical or stable mono-ring aromatic
            # In this case, we already have the aromatic form, so we're done
            pass

        # Check for isomorphism against the original molecule
        for i, newMol in enumerate(newMolList):
            if not keepIsomorphic and mol.isIsomorphic(newMol):
                # There will be at most one isomorphic molecule, since the new molecules have
                # already been checked against each other, so we can break after removing it
                del newMolList[i]
                break
            elif keepIsomorphic and mol.isIdentical(newMol):
                del newMolList[i]
                break
        # Add the newly generated structures to the original list
        # This is not optimal, but is a temporary measure to ensure compatability until other issues are fixed
        molList.extend(newMolList)

    # Generate remaining resonance structures
    methodList = populateResonanceAlgorithms(features)
    _generateResonanceStructures(molList, methodList, keepIsomorphic)

    return molList

def _generateResonanceStructures(molList, methodList, keepIsomorphic=False, copy=False):
    """
    Iteratively generate all resonance structures for a list of starting molecules using the specified methods.

    Args:
        molList             starting list of molecules
        methodList          list of resonance structure algorithms
        keepIsomorphic      if False, removes any structures that give isIsomorphic=True (default)
                            if True, only remove structures that give isIdentical=True
        copy                if False, append new resonance structures to input list (default)
                            if True, make a new list with all of the resonance structures
    """
    cython.declare(index=cython.int, molecule=Molecule, newMolList=list, newMol=Molecule, mol=Molecule)

    if copy:
        # Make a copy of the list so we don't modify the input list
        molList = molList[:]

    # Iterate over resonance isomers
    index = 0
    while index < len(molList):
        molecule = molList[index]
        newMolList = []

        for method in methodList:
            newMolList.extend(method(molecule))

        for newMol in newMolList:
            # Append to isomer list if unique
            for mol in molList:
                if not keepIsomorphic and mol.isIsomorphic(newMol):
                    break
                elif keepIsomorphic and mol.isIdentical(newMol):
                    break
            else:
                molList.append(newMol)

        # Move to next resonance isomer
        index += 1

    return molList

def generateAdjacentResonanceStructures(mol):
    """
    Generate all of the resonance structures formed by one allyl radical shift.

    Biradicals on a single atom are not supported.
    """
    cython.declare(isomers=list, paths=list, index=cython.int, isomer=Molecule)
    cython.declare(atom=Atom, atom1=Atom, atom2=Atom, atom3=Atom, bond12=Bond, bond23=Bond)
    cython.declare(v1=Vertex, v2=Vertex)
    
    isomers = []

    # Radicals
    if mol.isRadical():
        # Iterate over radicals in structure
        for atom in mol.vertices:
            paths = pathfinder.findAllDelocalizationPaths(atom)
            for atom1, atom2, atom3, bond12, bond23 in paths:
                # Adjust to (potentially) new resonance isomer
                atom1.decrementRadical()
                atom3.incrementRadical()
                bond12.incrementOrder()
                bond23.decrementOrder()
                # Make a copy of isomer
                isomer = mol.copy(deep=True)
                # Also copy the connectivity values, since they are the same
                # for all resonance forms
                for index in range(len(mol.vertices)):
                    v1 = mol.vertices[index]
                    v2 = isomer.vertices[index]
                    v2.connectivity1 = v1.connectivity1
                    v2.connectivity2 = v1.connectivity2
                    v2.connectivity3 = v1.connectivity3
                    v2.sortingLabel = v1.sortingLabel
                # Restore current isomer
                atom1.incrementRadical()
                atom3.decrementRadical()
                bond12.decrementOrder()
                bond23.incrementOrder()
                # Append to isomer list if unique
                isomer.updateAtomTypes(logSpecies=False)
                isomers.append(isomer)

    return isomers

def generateLonePairRadicalResonanceStructures(mol):
    """
    Generate all of the resonance structures formed by lone electron pair - radical shifts.
    """
    cython.declare(isomers=list, paths=list, index=cython.int, isomer=Molecule)
    cython.declare(atom=Atom, atom1=Atom, atom2=Atom)
    cython.declare(v1=Vertex, v2=Vertex)
    
    isomers = []

    # Radicals
    if mol.isRadical():
        # Iterate over radicals in structure
        for atom in mol.vertices:
            paths = pathfinder.findAllDelocalizationPathsLonePairRadical(atom)
            for atom1, atom2 in paths:
                # Adjust to (potentially) new resonance isomer
                atom1.decrementRadical()
                atom1.incrementLonePairs()
                atom1.updateCharge()
                atom2.incrementRadical()
                atom2.decrementLonePairs()
                atom2.updateCharge()
                # Make a copy of isomer
                isomer = mol.copy(deep=True)
                # Also copy the connectivity values, since they are the same
                # for all resonance forms
                for index in range(len(mol.vertices)):
                    v1 = mol.vertices[index]
                    v2 = isomer.vertices[index]
                    v2.connectivity1 = v1.connectivity1
                    v2.connectivity2 = v1.connectivity2
                    v2.connectivity3 = v1.connectivity3
                    v2.sortingLabel = v1.sortingLabel
                # Restore current isomer
                atom1.incrementRadical()
                atom1.decrementLonePairs()
                atom1.updateCharge()
                atom2.decrementRadical()
                atom2.incrementLonePairs()
                atom2.updateCharge()
                # Append to isomer list if unique
                isomer.updateAtomTypes(logSpecies=False)
                isomers.append(isomer)

    return isomers

def generateN5dd_N5tsResonanceStructures(mol):
    """
    Generate all of the resonance structures formed by shifts between N5dd and N5ts.
    """
    cython.declare(isomers=list, paths=list, index=cython.int, isomer=Molecule)
    cython.declare(atom=Atom, atom1=Atom, atom2=Atom, atom3=Atom)
    cython.declare(bond12=Bond, bond13=Bond)
    cython.declare(v1=Vertex, v2=Vertex)
    
    isomers = []
    
    # Iterate over nitrogen atoms in structure
    for atom in mol.vertices:
        paths = pathfinder.findAllDelocalizationPathsN5dd_N5ts(atom)
        for atom1, atom2, atom3, bond12, bond13, direction in paths:
            # from N5dd to N5ts
            if direction == 1:
                # Adjust to (potentially) new resonance isomer
                bond12.decrementOrder()
                bond13.incrementOrder()
                atom2.incrementLonePairs()
                atom3.decrementLonePairs()
                atom1.updateCharge()
                atom2.updateCharge()
                atom3.updateCharge()
                # Make a copy of isomer
                isomer = mol.copy(deep=True)
                # Also copy the connectivity values, since they are the same
                # for all resonance forms
                for index in range(len(mol.vertices)):
                    v1 = mol.vertices[index]
                    v2 = isomer.vertices[index]
                    v2.connectivity1 = v1.connectivity1
                    v2.connectivity2 = v1.connectivity2
                    v2.connectivity3 = v1.connectivity3
                    v2.sortingLabel = v1.sortingLabel
                # Restore current isomer
                bond12.incrementOrder()
                bond13.decrementOrder()
                atom2.decrementLonePairs()
                atom3.incrementLonePairs()
                atom1.updateCharge()
                atom2.updateCharge()
                atom3.updateCharge()
                # Append to isomer list if unique
                isomer.updateAtomTypes(logSpecies=False)
                isomers.append(isomer)
            
            # from N5ts to N5dd
            if direction == 2:
                # Adjust to (potentially) new resonance isomer
                bond12.decrementOrder()
                bond13.incrementOrder()
                atom2.incrementLonePairs()
                atom3.decrementLonePairs()
                atom1.updateCharge()
                atom2.updateCharge()
                atom3.updateCharge()
                # Make a copy of isomer
                isomer = mol.copy(deep=True)
                # Also copy the connectivity values, since they are the same
                # for all resonance forms
                for index in range(len(mol.vertices)):
                    v1 = mol.vertices[index]
                    v2 = isomer.vertices[index]
                    v2.connectivity1 = v1.connectivity1
                    v2.connectivity2 = v1.connectivity2
                    v2.connectivity3 = v1.connectivity3
                    v2.sortingLabel = v1.sortingLabel
                # Restore current isomer
                bond12.incrementOrder()
                bond13.decrementOrder()
                atom2.decrementLonePairs()
                atom3.incrementLonePairs()
                atom1.updateCharge()
                atom2.updateCharge()
                atom3.updateCharge()
                # Append to isomer list if unique
                isomer.updateAtomTypes(logSpecies=False)
                isomers.append(isomer)
                
    return isomers

def generateAromaticResonanceStructures(mol, features=None):
    """
    Generate the aromatic form of the molecule. For radicals, generates the form with the most aromatic rings.
    
    Returns result as a list.
    In most cases, only one structure will be returned.
    In certain cases where multiple forms have the same number of aromatic rings, multiple structures will be returned.
    If there's an error (eg. in RDKit) it just returns an empty list.
    """
    cython.declare(molecule=Molecule, rings=list, aromaticBonds=list, kekuleList=list, maxNum=cython.int, molList=list,
                   newMolList=list, ring=list, bond=Bond, order=float, originalBonds=list, originalOrder=list,
                   i=cython.int, counter=cython.int)

    if features is None:
        features = analyzeMolecule(mol)

    if not features['isCyclic']:
        return []

    molecule = mol.copy(deep=True)

    # First get all rings in the molecule
    rings = molecule.getAllSimpleCyclesOfSize(6)

    # Then determine which ones are aromatic
    aromaticBonds = molecule.getAromaticRings(rings)[1]

    # If the species is a radical and the number of aromatic rings is less than the number of total rings,
    # then there is a chance that the radical can be shifted to a location that increases the number of aromatic rings.
    if (features['isRadical'] and not features['isArylRadical']) and (len(aromaticBonds) < len(rings)):
        if molecule.isAromatic():
            kekuleList = generateKekuleStructure(molecule)
        else:
            kekuleList = [molecule]
        _generateResonanceStructures(kekuleList, [generateAdjacentResonanceStructures])

        maxNum = 0
        molList = []

        # Iterate through the adjacent resonance structures and keep the structures with the most aromatic rings
        for mol0 in kekuleList:
            aromaticBonds = mol0.getAromaticRings()[1]
            if len(aromaticBonds) > maxNum:
                maxNum = len(aromaticBonds)
                molList = [(mol0, aromaticBonds)]
            elif len(aromaticBonds) == maxNum:
                molList.append((mol0, aromaticBonds))
    else:
        # Otherwise, it is not possible to increase the number of aromatic rings by moving electrons,
        # so go ahead with the inputted form of the molecule
        molList = [(molecule, aromaticBonds)]

    newMolList = []

    # Generate the aromatic resonance structure(s)
    for mol0, aromaticBonds in molList:
        if not aromaticBonds:
            continue
        # Save original bond orders in case this doesn't work out
        originalBonds = []
        for ring in aromaticBonds:
            originalOrder = []
            for bond in ring:
                originalOrder.append(bond.order)
            originalBonds.append(originalOrder)
        # Change bond types to benzene bonds for all aromatic rings
        for ring in aromaticBonds:
            for bond in ring:
                bond.order = 1.5

        try:
            mol0.updateAtomTypes(logSpecies=False)
        except AtomTypeError:
            # If this didn't work the first time, then there might be a ring that is not actually aromatic
            # Reset our changes
            for ring, originalOrder in itertools.izip(aromaticBonds, originalBonds):
                for bond, order in itertools.izip(ring, originalOrder):
                    bond.order = order
            # Try to make each ring aromatic, one by one
            i = 0
            counter = 0
            while i < len(aromaticBonds) and counter < 2*len(aromaticBonds):
                counter += 1
                originalOrder = []
                for bond in aromaticBonds[i]:
                    originalOrder.append(bond.order)
                    bond.order = 1.5
                try:
                    mol0.updateAtomTypes(logSpecies=False)
                except AtomTypeError:
                    # This ring could not be made aromatic, possibly because it depends on other rings
                    # Undo changes
                    for bond, order in itertools.izip(aromaticBonds[i], originalOrder):
                        bond.order = order
                    # Move it to the end of the list, and go on to the next ring
                    aromaticBonds.append(aromaticBonds.pop(i))
                    continue
                else:
                    # We're done with this ring, so go on to the next ring
                    i += 1
            # If we didn't end up making any of the rings aromatic, then this molecule is not actually aromatic
            if i == 0:
                # Move onto next molecule in the list
                continue

        for mol1 in newMolList:
            if mol1.isIsomorphic(mol0):
                break
        else:
            newMolList.append(mol0)

    return newMolList

def generateKekuleStructure(mol):
    """
    Generate a kekulized (single-double bond) form of the molecule.
    The specific arrangement of double bonds is non-deterministic, and depends on RDKit.

    Returns a single Kekule structure as an element of a list of length 1.
    If there's an error (eg. in RDKit) then it just returns an empty list.
    """
    cython.declare(atom=Atom, molecule=Molecule)

    for atom in mol.atoms:
        if atom.atomType.label == 'Cb' or atom.atomType.label == 'Cbf':
            break
    else:
        return []

    molecule = mol.copy(deep=True)

    try:
        kekulize(molecule)
    except AtomTypeError:
        return []

    return [molecule]

def generateOppositeKekuleStructure(mol):
    """
    Generate the Kekule structure with opposite single/double bond arrangement
    for single ring aromatics.

    Returns a single Kekule structure as an element of a list of length 1.
    """

    # This won't work with the aromatic form of the molecule
    if mol.isAromatic():
        return []

    molecule = mol.copy(deep=True)

    aromaticBonds = molecule.getAromaticRings()[1]

    # We can only do this for single ring aromatics for now
    if len(aromaticBonds) != 1:
        return []

    numS = 0
    numD = 0
    for bond in aromaticBonds[0]:
        if bond.isSingle():
            numS += 1
            bond.order = 2
        elif bond.isDouble():
            numD += 1
            bond.order = 1
        else:
            # Something is wrong: there is a bond that is not single or double
            return []

    if numS != 3 or numD != 3:
        return []

    try:
        molecule.updateAtomTypes()
    except AtomTypeError:
        return []
    else:
        return [molecule]

def generateIsomorphicResonanceStructures(mol):
    """
    Select the resonance isomer that is isomorphic to the parameter isomer, with the lowest unpaired
    electrons descriptor.

    We generate over all resonance isomers (non-isomorphic as well as isomorphic) and retain isomorphic
    isomers.

    WIP: do not generate aromatic resonance isomers.
    """

    cython.declare(isomorphic_isomers=list,\
                   isomers=list,
                    )

    cython.declare(isomer=Molecule,\
                   newIsomer=Molecule,\
                   isom=Molecule
                   )

    cython.declare(index=int)

    isomorphic_isomers = [mol]# resonance isomers that are isomorphic to the parameter isomer.

    isomers = [mol]

    # Iterate over resonance isomers
    index = 0
    while index < len(isomers):
        isomer = isomers[index]
        
        newIsomers = []
        for algo in populateResonanceAlgorithms():
            newIsomers.extend(algo(isomer))
        
        for newIsomer in newIsomers:
            # Append to isomer list if unique
            for isom in isomers:
                if isom.copy(deep=True).isIsomorphic(newIsomer.copy(deep=True)):
                    isomorphic_isomers.append(newIsomer)
                    break
            else:
                isomers.append(newIsomer)        
                    
        # Move to next resonance isomer
        index += 1

    return isomorphic_isomers


def generateClarStructures(mol):
    """
    Generate Clar structures for a given molecule.

    Returns a list of :class:`Molecule` objects corresponding to the Clar structures.
    """
    cython.declare(output=list, molList=list, newmol=Molecule, aromaticRings=list, bonds=list, solution=list,
                   y=list, x=list, index=cython.int, bond=Bond, ring=list)

    if not mol.isCyclic():
        return []

    try:
        output = _clarOptimization(mol)
    except ILPSolutionError:
        # The optimization algorithm did not work on the first iteration
        return []

    molList = []

    for newmol, aromaticRings, bonds, solution in output:

        # The solution includes a part corresponding to rings, y, and a part corresponding to bonds, x, using
        # nomenclature from the paper. In y, 1 means the ring as a sextet, 0 means it does not.
        # In x, 1 corresponds to a double bond, 0 either means a single bond or the bond is part of a sextet.
        y = solution[0:len(aromaticRings)]
        x = solution[len(aromaticRings):]

        # Apply results to molecule - double bond locations first
        for index, bond in enumerate(bonds):
            if x[index] == 0:
                bond.order = 1 # single
            elif x[index] == 1:
                bond.order = 2 # double
            else:
                raise ValueError('Unaccepted bond value {0} obtained from optimization.'.format(x[index]))

        # Then apply locations of aromatic sextets by converting to benzene bonds
        for index, ring in enumerate(aromaticRings):
            if y[index] == 1:
                _clarTransformation(newmol, ring)

        try:
            newmol.updateAtomTypes()
        except AtomTypeError:
            pass
        else:
            molList.append(newmol)

    return molList


def _clarOptimization(mol, constraints=None, maxNum=None):
    """
    Implements linear programming algorithm for finding Clar structures. This algorithm maximizes the number
    of Clar sextets within the constraints of molecular geometry and atom valency.

    Returns a list of valid Clar solutions in the form of a tuple, with the following entries:
        [0] Molecule object
        [1] List of aromatic rings
        [2] List of bonds
        [3] Optimization solution

    The optimization solution is a list of boolean values with sextet assignments followed by double bond assignments,
    with indices corresponding to the list of aromatic rings and list of bonds, respectively.

    Method adapted from:
        Hansen, P.; Zheng, M. The Clar Number of a Benzenoid Hydrocarbon and Linear Programming.
            J. Math. Chem. 1994, 15 (1), 93–107.
    """
    cython.declare(molecule=Molecule, aromaticRings=list, exo=list, l=cython.int, m=cython.int, n=cython.int,
                   a=list, objective=list, status=cython.int, solution=list, innerSolutions=list)

    from lpsolve55 import lpsolve

    # Make a copy of the molecule so we don't destroy the original
    molecule = mol.copy(deep=True)

    aromaticRings = molecule.getAromaticRings()[0]

    if not aromaticRings:
        return []

    # Get list of atoms that are in rings
    atoms = set()
    for ring in aromaticRings:
        atoms.update(ring)
    atoms = list(atoms)

    # Get list of bonds involving the ring atoms, ignoring bonds to hydrogen
    bonds = set()
    for atom in atoms:
        bonds.update([atom.bonds[key] for key in atom.bonds.keys() if key.isNonHydrogen()])
    bonds = list(bonds)

    # Identify exocyclic bonds, and save their bond orders
    exo = []
    for bond in bonds:
        if bond.atom1 not in atoms or bond.atom2 not in atoms:
            if bond.isDouble():
                exo.append(1)
            else:
                exo.append(0)
        else:
            exo.append(None)

    # Dimensions
    l = len(aromaticRings)
    m = len(atoms)
    n = l + len(bonds)

    # Connectivity matrix which indicates which rings and bonds each atom is in
    # Part of equality constraint Ax=b
    a = []
    for atom in atoms:
        inRing = [1 if atom in ring else 0 for ring in aromaticRings]
        inBond = [1 if atom in [bond.atom1, bond.atom2] else 0 for bond in bonds]
        a.append(inRing + inBond)

    # Objective vector for optimization: sextets have a weight of 1, double bonds have a weight of 0
    objective = [1] * l + [0] * len(bonds)

    # Solve LP problem using lpsolve
    lp = lpsolve('make_lp', m, n)               # initialize lp with constraint matrix with m rows and n columns
    lpsolve('set_verbose', lp, 2)               # reduce messages from lpsolve
    lpsolve('set_obj_fn', lp, objective)        # set objective function
    lpsolve('set_maxim', lp)                    # set solver to maximize objective
    lpsolve('set_mat', lp, a)                   # set left hand side to constraint matrix
    lpsolve('set_rh_vec', lp, [1] * m)          # set right hand side to 1 for all constraints
    lpsolve('set_constr_type', lp, ['='] * m)   # set all constraints as equality constraints
    lpsolve('set_binary', lp, [True] * n)       # set all variables to be binary

    # Constrain values of exocyclic bonds, since we don't want to modify them
    for i in range(l, n):
        if exo[i - l] is not None:
            # NOTE: lpsolve indexes from 1, so the variable we're changing should be i + 1
            lpsolve('set_bounds', lp, i + 1, exo[i - l], exo[i - l])

    # Add constraints to problem if provided
    if constraints is not None:
        for constraint in constraints:
            try:
                lpsolve('add_constraint', lp, constraint[0], '<=', constraint[1])
            except:
                logging.error('Unable to add constraint: {0} <= {1}'.format(constraint[0], constraint[1]))
                logging.error('Cannot complete Clar optimization for {0}.'.format(str(mol)))
                logging.error(mol.toAdjacencyList())
                raise

    status = lpsolve('solve', lp)
    objVal, solution = lpsolve('get_solution', lp)[0:2]
    lpsolve('delete_lp', lp)  # Delete the LP problem to clear up memory

    # Check that optimization was successful
    if status != 0:
        raise ILPSolutionError('Optimization could not find a valid solution.')

    # Check that we the result contains at least one aromatic sextet
    if objVal == 0:
        return []

    # Check that the solution contains the maximum number of sextets possible
    if maxNum is None:
        maxNum = objVal  # This is the first solution, so the result should be an upper limit
    elif objVal < maxNum:
        raise ILPSolutionError('Optimization obtained a sub-optimal solution.')

    if any([x != 1 and x != 0 for x in solution]):
        raise ILPSolutionError('Optimization obtained a non-integer solution.')

    # Generate constraints based on the solution obtained
    y = solution[0:l]
    new_a = y + [0] * len(bonds)
    new_b = sum(y) - 1
    if constraints is not None:
        constraints.append((new_a, new_b))
    else:
        constraints = [(new_a, new_b)]

    # Run optimization with additional constraints
    try:
        innerSolutions = _clarOptimization(mol, constraints=constraints, maxNum=maxNum)
    except ILPSolutionError:
        innerSolutions = []

    return innerSolutions + [(molecule, aromaticRings, bonds, solution)]


def _clarTransformation(mol, aromaticRing):
    """
    Performs Clar transformation for given ring in a molecule, ie. conversion to aromatic sextet.

    Args:
        mol             a :class:`Molecule` object
        aromaticRing    a list of :class:`Atom` objects corresponding to an aromatic ring in mol

    This function directly modifies the input molecule and does not return anything.
    """
    cython.declare(bondList=list, i=cython.int, atom1=Atom, atom2=Atom, bond=Bond)

    bondList = []

    for i, atom1 in enumerate(aromaticRing):
        for atom2 in aromaticRing[i + 1:]:
            if mol.hasBond(atom1, atom2):
                bondList.append(mol.getBond(atom1, atom2))

    for bond in bondList:
        bond.order = 1.5


class ILPSolutionError(Exception):
    """
    An exception to be raised when solving an integer linear programming problem if a solution
    could not be found or the solution is not valid. Can pass a string to indicate the reason
    that the solution is invalid.
    """
    pass
