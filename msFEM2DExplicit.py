__author__="Ning Guo, ceguo@connect.ust.hk; Hongyang Cheng, d132535@hiroshima-u.ac.jp"
__supervisor__="Jidong Zhao, jzhao@ust.hk"
__institution__="The Hong Kong University of Science and Technology"

""" 2D model for Hierachical/Concurrent multiscale simulation
which implements explicit time integration in FEM
to solve a nonlinear boundary valued problem. 
Stress and boundary conditions are obtained from RVEs(internal DE) and
interface discrete cylinder elements
by calling simDEM modules"""

# import Escript modules
import esys.escript as escript
from esys.escript import util
from esys.escript.linearPDEs import LinearPDE,SolverOptions
# import YADE modules
from simDEM import *
# other python modules
from itertools import repeat
from scipy.io import mmread
from scipy.sparse.linalg import eigs

""" function to return pool for parallelization
    supporting both MPI (experimental) on distributed
    memory and multiprocessing on shared memory.
"""
def get_pool(mpi=False,threads=1):
   if mpi: # using MPI
      from mpipool import MPIPool
      pool = MPIPool()
      pool.start()
      if not pool.is_master():
         sys.exit(0)
   elif threads>1: # using multiprocessing
      from multiprocessing import Pool
      pool = Pool(processes=threads,maxtasksperchild=50000)
   else:
      raise RuntimeError,"Wrong arguments: either mpi=True or threads>1."
   return pool

### Multiscale class modified by Hongyang Cheng ###

class MultiScale(object):
   """
   problem description:
   D_ij u_j = -X_{ij,j} + Y_i
   Neumann boundary: d_ij u_j = n_j X_{ij} + y_i
   Dirichlet boundary: u_i = r_i where q_i > 0
   :var u: unknown vector, displacement
   :var X: old/current stress tensor
   :var Y: vector, body force
   :var y: vector, Neumann bc traction
   :var q: vector, Dirichlet bc mask
   :var r: vector, Dirichlet bc value
   """
   def __init__(self,domain,dim,ng=1,useMPI=False,np=1,rho=2.35e3,mIds=False,\
                FEDENodeMap=False,DE_ext=False,FEDEBoundMap=False,conf=False):
      """
      initialization of the problem, i.e. model constructor
      :param domain: type Domain, domain of the problem
      :param ng: type integer, number of Gauss points
      :param useMPI: type boolean, use MPI or not
      :param np: type integer, number of processors
      :param rho: type float, density of material
      :param mIds: a list contains membrane node IDs
      :param FEDENodeMap: a dictionary with FE and DE boundary node IDs in keys and values
      :param DE_ext: name of file which saves initial state of exterior DE domain
      :param FEDEBoundMap: a dictionary with FE and DE boundary element IDs in keys and values (deprecated)
      :param conf: type float, conf pressure on membrane
      """
      self.__domain=domain
      self.__pde=LinearPDE(self.__domain,numEquations=dim,numSolutions=dim)
      self.__pde.getSolverOptions().setSolverMethod(SolverOptions.HRZ_LUMPING)
      self.__pde.setSymmetryOn()
      self.__numGaussPoints=ng
      self.__rho=rho
      self.__mIds=mIds
      self.__FEDENodeMap=FEDENodeMap
      self.__FEDEBoundMap=FEDEBoundMap
      self.__conf=conf
      self.__pool=get_pool(mpi=useMPI,threads=np)
      self.__scenes=self.__pool.map(initLoad,range(ng))
      self.__strain=escript.Tensor(0,escript.Function(self.__domain))
      self.__stress=escript.Tensor(0,escript.Function(self.__domain))
      self.__u=escript.Vector(0,escript.Solution(self.__domain))
      self.__u_last=escript.Vector(0,escript.Solution(self.__domain))
      self.__u_t=escript.Vector(0,escript.Solution(self.__domain))
      self.__dt=0
      # ratio between timesteps in internal DE and FE domain
      self.__nsOfDE_int=1
      # if FEDENodeMap is given, employ exterior DE domain
      if self.__FEDENodeMap:
         self.__sceneExt=self.__pool.apply(initLoadExt,(DE_ext,))
         # ratio between timesteps in external DE and FE domain 
         self.__nsOfDE_ext=1
         # get interface nodal forces as boundary condition
         self.__FEf = self.__pool.apply(initNbc,(self.__sceneExt,self.__conf,mIds,FEDENodeMap))
         """
         # get interface nodal tractions as boundary condition (deprecated)
         self.__Nbc=escript.Vector(0,escript.Solution(self.__domain))
         FENbc = self.__pool.apply(initNbc,(self.__sceneExt,self.__conf,mIds,FEDENodeMap))
         for FEid in FENbc.keys():
            self.__Nbc.setValueOfDataPoint(FEid,FENbc[FEid])
         """
      # get stress tensor at material points
      s = self.__pool.map(getStress2D,self.__scenes)
      for i in xrange(ng):
         self.__stress.setValueOfDataPoint(i,s[i])
                     
   def initialize(self, b=escript.Data(), f=escript.Data(), specified_u_mask=escript.Data(), specified_u_val=escript.Data(), dt=0):
      """
      initialize the model for each time step, e.g. assign parameters
      :param b: type vector, body force on FunctionSpace, e.g. gravity
      :param f: type vector, boundary traction on FunctionSpace (FunctionOnBoundary)
      :param specified_u_mask: type vector, mask of location for Dirichlet boundary
      :param specified_u_val: type vector, specified displacement for Dirichlet boundary
      """
      self.__pde.setValue(Y=b,y=f,q=specified_u_mask,r=specified_u_val)
      # if FEDENodeMap is given
      if self.__FEDENodeMap:
			# assign dt_FE/dt_DE_ext to self.__nsOfDE_ext
         dt_ext = self.__pool.apply(getScenetDt,(self.__sceneExt,))
         self.__nsOfDE_ext = int(round(dt/dt_ext))
         print "Ratio between time step in FE and exterior DE domain: %1.1e"%self.__nsOfDE_ext
      dt_int = self.__pool.map(getScenetDt,self.__scenes)
      # assign a list of dt_FE/dt_DE_int to self.__nsOfDE_int
      self.__nsOfDE_int = numpy.round(numpy.array(dt)/dt_int).astype(int)
      print "Maximum ratio between time step in FE and interior DE domains: %1.1e"%max(self.__nsOfDE_int)
      if dt == 0:
         raise RuntimeError,"Time step in FE domain is not given"
      self.__dt = dt
      print "Time step in FE domain:%1.1e"%self.__dt

   def getDomain(self):
      """
      return model domain
      """
      return self.__domain

## below is Ning Guo's original script ##
    
   def getCurrentPacking(self,pos=(),time=0,prefix=''):
      if len(pos) == 0:
         # output all Gauss points packings
         self.__pool.map(outputPack,zip(self.__scenes,repeat(time),repeat(prefix)))
      else:
         # output selected Gauss points packings
         scene = [self.__scenes[i] for i in pos]
         self.__pool.map(outputPack,zip(scene,repeat(time),repeat(prefix)))
   
   def getLocalVoidRatio(self):
      void=escript.Scalar(0,escript.Function(self.__domain))
      e = self.__pool.map(getVoidRatio2D,self.__scenes)
      for i in xrange(self.__numGaussPoints):
         void.setValueOfDataPoint(i,e[i])
      return void
   
   def getLocalAvgRotation(self):
      rot=escript.Scalar(0,escript.Function(self.__domain))
      r = self.__pool.map(avgRotation2D,self.__scenes)
      for i in xrange(self.__numGaussPoints):
         rot.setValueOfDataPoint(i,r[i])
      return rot
   
   def getLocalFabric(self):
      fabric=escript.Tensor(0,escript.Function(self.__domain))
      f = self.__pool.map(getFabric2D,self.__scenes)
      for i in xrange(self.__numGaussPoints):
         fabric.setValueOfDataPoint(i,f[i])
      return fabric
      
   def getCurrentStress(self):
      """
      return current stress
      type: Tensor on FunctionSpace
      """
      return self.__stress

   def getCurrentTangent(self):
      """
      return current tangent operator
      type Tensor4 on FunctionSpace
      """
      t=escript.Tensor4(0,escript.Function(self.__domain))
      st = self.__pool.map(getStressAndTangent2D,self.__scenes)
      for i in xrange(self.__numGaussPoints):
         t.setValueOfDataPoint(i,st[i][1])
      return t
      
   def getCurrentStrain(self):
      """
      return current strain
      type: Tensor on FunctionSpace
      """
      return self.__strain
   
   def exitSimulation(self):
      """finish the whole simulation, exit"""
      self.__pool.close()

## below is modified by Hongyang Cheng ##

   def setRHS(self,X,Y=escript.Data()):
      """
      set right hande side of PDE, including X_{ij} and Y_i
      X: stress tensor at (n) time step
      Y: vector, (equivalent) body force at (n) time step
      Note that boundary force, if any, is set inhere
      """
      # apply internal stress and equivalent body force
      self.__pde.setValue(X=X, Y=Y)
      # if exterior DE domain is given
      if self.__FEDENodeMap:
         FEf = self.getFEf()
         rhs = self.__pde.getRightHandSide()
      # !!!!!! apply boundary force to the right hande side of PDE
         for FEid in FEf.keys():
            rhs_i = rhs.getTupleForDataPoint(FEid)
            rhs_i_new = [sum(f) for f in zip(rhs_i,FEf[FEid])]
            rhs.setValueOfDataPoint(FEid,rhs_i_new)
      # consider Neumann boundary conditions
      rhs -= rhs*self.__pde.getCoefficient('q')

      """
      # !!!!!! apply boundary traction to the right hand side of PDE (deprecated)
         self.__pde.setValue(X=X, Y=Y, y=self.__Nbc)
      """
   
   def solve(self,damp=.2,dynRelax=False):
      """
      solve the equation of motion in centeral difference scheme
      """
      # get initial coordinate
      x = self.getDomain().getX()
      # density matrix
      kron = util.kronecker(self.getDomain().getDim())
      rho = self.__rho*kron
      # stress at (n) time step
      stress_last = self.getCurrentStress()
      # set coefficients for ODE
      self.__pde.setValue(D=rho)
      self.setRHS(X=-stress_last)
      # solve acceleration at (n) time step from ODE
      u_tt = self.__pde.getSolution()
      # update velocity at (n+1/2) time step
      self.__u_t = 1./(2.+damp*self.__dt)*((2.-damp*self.__dt)*self.__u_t + 2.*self.__dt*u_tt)
      # get displacement increment
      du = self.__dt*self.__u_t
      # update strain
      D = util.grad(du)
      self.__strain += D
      # update displacement and domain geometry at (n+1) time step
      self.__u += du
      self.__domain.setX(x+du)

      # !!!!!! apply displacement increment and get boundary force from DE exterior domain, if any
      if self.__FEDENodeMap:
         DEdu = self.getBoundaryDisplacement(du)
         """
         # apply boundary velocity and get boundary traction (deprecated)
         self.__Nbc,self.__sceneExt=self.applyDisplIncrement_getTractionDEM(DEdu=DEdu)
         """
         # apply boundary velocity and return an asynResult object
         arFEfAndSceneExt=self.applyDisplIncrement_getForceDEM(DEdu=DEdu,dynRelax=dynRelax)

      # !!!!!! update stress and scenes from DEM part using strain at (n+1) time step
      self.__stress,self.__scenes = self.applyStrain_getStressDEM(st=D,dynRelax=dynRelax)
      """
      # !!!!!! update scenes and return asynResult objects (deprecated)
      arScenes = self.applyStrain(st=D)
		# !!!!!! retrieve data from asyncResults
      # update interior DE scenes
      self.__scenes = arScenes.get()
      # assign new stresses to gauss points from interior DE scenes
      s = self.__pool.map(getStress2D,self.__scenes)
      for i in xrange(self.__numGaussPoints):
         self.__stress.setValueOfDataPoint(i,s[i])
      """
       # if exterior DE domain is given, update scene and boundary force
      if self.__FEDENodeMap:
         self.__FEf,self.__sceneExt = arFEfAndSceneExt.get()
      return self.__u, self.__u_t
            
   """
   apply strain to DEM packing and return a result object
   """
   def applyStrain(self,st=escript.Data()):
      st = st.toListOfTuples()
      st = numpy.array(st).reshape(-1,4)
      # load DEM packing with strain
      arScenes = self.__pool.map_async(shear2D,zip(self.__scenes,st,self.__nsOfDE_int))
      return arScenes

   """
   apply strain to DEM packing and get stress
   """
   def applyStrain_getStressDEM(self,st=escript.Data(),dynRelax=False):
      st = st.toListOfTuples()
      st = numpy.array(st).reshape(-1,4)
      stress = escript.Tensor(0,escript.Function(self.__domain))
      # load DEM packing with strain
      scenes = self.__pool.map(shear2D,zip(self.__scenes,st,self.__nsOfDE_int,repeat(dynRelax)))
      # return homogenized stress
      s = self.__pool.map(getStress2D,scenes)
      for i in xrange(self.__numGaussPoints):
         stress.setValueOfDataPoint(i,s[i])
      return stress,scenes

   #~ def applyDisplIncrement_getTractionDEM(self,DEdu=escript.Data()):
      #~ """
      #~ apply displacement increment to the external DE domain,
      #~ and get boundary traction from DE interface nodes
      #~ """
      #~ FENbc, sceneExt = self.__pool.apply( \
                        #~ moveInterface_getTraction2D,(self.__sceneExt,self.__conf,DEdu, \
                        #~ self.__mIds,self.__FEDENodeMap,self.__nsOfDE_ext))
      #~ Nbc=escript.Vector(0,escript.Solution(self.getDomain()))
      #~ for FEid in self.__FEDENodeMap.keys():
         #~ Nbc.setValueOfDataPoint(FEid,FENbc[FEid])
      #~ return Nbc, sceneExt
      
   def applyDisplIncrement_getForceDEM(self,DEdu=escript.Data(),dynRelax=False):
      """
      apply displacement increment to the external DE domain,
      and get boundary force from DE interface nodes
      """
      arFEfAndSceneExt = self.__pool.apply_async( \
                            moveInterface_getForce2D,(self.__sceneExt,self.__conf,DEdu,dynRelax, \
                            self.__mIds,self.__FEDENodeMap,self.__nsOfDE_ext))
      return arFEfAndSceneExt
      
   def getBoundaryDisplacement(self,du):
      """
      get a dictionary contains DE interface node ids and displacement increment
      """
      # exchange FE-node ids (keys) with DE-node ids (values)
      DEdu = dict((v,k) for k,v in self.__FEDENodeMap.iteritems())
      for key in DEdu.keys():
         FEid = DEdu[key]
         DEdu[key] = du.getTupleForDataPoint(FEid)
      return DEdu
   
   def VTKExporter(self,vtkDir='./',t=0):
      """
      export interactions in all DE scenes using vtk format
      """
      # export interactions in RVEs
      #~ self.__pool.map(exportInt,zip(self.__scenes,repeat(vtkDir),repeat(t)))
      # export tensile forces in membrane
      self.__pool.apply(exportExt,(self.__sceneExt,self.__mIds,vtkDir,t))
                    
   def getPDE(self):
      """
      return partial differential equation
      """
      return self.__pde
      
   def getSceneExt(self):
      """
      return scene of exterior DE domain, if any
      """
      return self.__sceneExt
   
   def getNbc(self):
      """
      return Neumann boundary condition converted from exterior DE domain
      """
      if self.__FEDENodeMap:
         Nbc = self.__Nbc
      else:
         Nbc = self.__pde.getCoefficient("y")
      return Nbc

   def getFEf(self):
      """
      return boundary forces converted from exterior DE domain
      """
      if self.__FEDENodeMap:
         FEf = self.__FEf
      else:
         raise RuntimeError,"No exterior DE domain!"
      return FEf

   def getNsOfDE_int(self):
      """
      return a list of numbers of interior DE simulations per FE domain simulation
      """     
      return self.__nsOfDE_int

   def getNsOfDE_ext(self):
      """
      return number of exterior DE simulations per FE domain simulation
      """     
      return self.__nsOfDE_ext

   def getMaxEigenvalue(self):
      """
      return the max eigenvalue of the model
      type: float
      """
      dom = self.getDomain()
      dim = dom.getDim()
      pdeKu_P = LinearPDE(dom,numEquations=dim,numSolutions=dim)
      T = self.getCurrentTangent()
      pdeKu_P.setValue(A=T)
      pdeKu_P.getOperator().saveMM('tempMTX.mtx')
      mtx = mmread('tempMTX.mtx')
      maxEigVal = max(eigs(mtx,k=1,return_eigenvectors=False,which='LR'))
      return maxEigVal.real

   def getBoundaryNodesPositions(self,tag_name ="bound"):
      """
      get a dictionary contains FE boundary node IDs and corresponding positions
      use data on "Solution" FunctionSpace to export FE node IDs (DataPoint IDs)
      note that though "ContinousFunction" and "Solution" FunctionSpace have save number of DataPoints,
      their numbering order are not the same!!!
      Therefore, always use coordiates and IDs of DataPoints on "Solution" for data communication
      """
      tag = self.getDomain().getTag(tag_name)
      # get "Solution" FunctinonSpace
      fs = escript.Solution(self.getDomain())
      # get coordinates with "Solution" FunctionSpace numbering order
      # fs and fs.getX().getFunctionSpace() are the same
      x = fs.getX()
      # get domain FunctionSpace, note below is different from fs.getX().getFunctionSpace()
      domFS = fs.getDomain().getX().getFunctionSpace()
      num = x.getNumberOfDataPoints()
      pos = {}
      for i in xrange(num):
         refId = fs.getReferenceIDFromDataPointNo(i)
         # domain FunctionSpace DataPoint IDs = RefId-1
         if domFS.getTagFromDataPointNo(refId-1) == tag:
            pos[i] = x.getTupleForDataPoint(i)
      return pos

   def getBoundaryDataPointIDsAndRefIDs(self):
      """
      get a dictionary contains data point IDs and reference IDs on BoundaryOnFunction
      numbering order of reference IDs and interface elements need to be consistent
      """
      # get "FunctionOnBoundary" FunctionSpace
      bfs = escript.FunctionOnBoundary(self.getDomain())
      # convert discrete reference IDs to continous numbers
      num = bfs.getX().getNumberOfDataPoints()
      FEDEBoundMap = {}; n = 0
      for i in xrange(num):
         refID = bfs.getReferenceIDFromDataPointNo(i)
         if n != refID: newID = 2*(refID-1)
         else: newID = 2*(refID-1)+1
         # save DataPoint IDs in keys and converted newIDs in values
         n = refID; FEDEBoundMap[i] = newID
      return FEDEBoundMap

   """
   # solve the equation of motion in verlet scheme (deprecated)
   def solve(self,damp=.2,dt=1.e-6):
      # get initial coordinate
      x = self.getDomain().getX()
      # density matrix      
      kron = kronecker(self.__domain.getDim())
      rho = self.__rho*kron
      # stress and velocity at (n) time step
      stress_last = self.getCurrentStress()
      v_last = (self.__u-self.__u_last)/dt
      # set cooefficients for ODE
      self.__pde.setValue(D=rho*(1.+damp*dt), X=-stress_last, Y=-self.__rho*damp*v_last)
      # solve acceleration at (n+1) time step from ODE
      a = self.__pde.getSolution()
      # get displacement increment
      du = self.__u-self.__u_last+a*dt**2.
      # update strain
      D = util.grad(du)
      self.__strain += D
      # update stress and scenes from DEM part using strain at t_(n+1) if L2(D) != 0
      self.__stress,self.__scenes = self.applyStrain_getStressDEM(st=D)
      # update displacement at (n) and (n+1) time step
      self.__u_last = self.__u[:]
      self.__u += du
      # update domain geometry
      self.__domain.setX(x+du)
      return self.__u, du/dt
   """
