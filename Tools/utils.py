from pathlib import Path
import sys
script_dir = Path(__file__).resolve().parent
grandparent_dir = script_dir.parent.parent
if str(grandparent_dir) not in sys.path:
    sys.path.append(str(grandparent_dir))
import numpy as np
from scipy.interpolate import splrep, splev
from scipy import stats
import matplotlib.pyplot as plt
import pandas as pd
import os
import torch
import enum
from typing import List
from datetime import datetime
import json
import yaml
import copy
from skimage.measure import CircleModel,LineModelND, ransac
from sklearn.linear_model import LinearRegression
from sklearn.linear_model import RANSACRegressor
from matplotlib.patches import Polygon
import time
import logging

# if torch.cuda.is_available():
#     device = torch.device('cuda')
#     print("Using GPU")
#     torch.set_default_dtype(torch.float32)  # Set default data type
#     torch.set_default_device('cuda')  # Set default device (optional)
#     #Setting default device to 'cuda' causes some problems with the spline functions that try to turn tensors into numpy inside the functions
#     #therefore, as a WA i set this to cpu before those functions. These functions dont need to be backproped through
# else:
#     device = torch.device('cpu')
#     print("Using CPU")

#  https://skisickness.com/2010/04/relativistic-kinematics-calculator/
class SS_VARIABLE(enum.Enum):
    X = 0
    Y = 1
    Z = 2
    Vx = 3
    Vy = 4
    Vz = 5
    Px = 3
    Py = 4
    Pz = 5

class System_Mode(enum.Enum):
    FW_ONLY = "FW"
    BW_ONLY = "BW"
    HEAD_ONLY = "HEAD"
    FW_BW= "FW + BW"
    BW_HEAD= "BW + HEAD"
    FW_BW_HEAD= "FW + BW + HEAD"


class Trajectory_Source(enum.Enum):
    Amit_Simulated = 0
    Yassid_Simulated = 1
    Experiment = 2

class Trajectory_SS_Type(enum.Enum):
    FTT = "Fine Tuned Traj"
    GTT = "Ground Truth Traj"
    OT = "Observed Traj"
    Estimated_FW = "Estimated FW SS"
    Estimated_BW = "Estimated BW SS"

class CONFIG():
    def __init__(self,config_path) -> None:
        self.parse_config(config_path)
    def parse_config(self,config_path):
        ext = config_path.split('.')[-1]
        assert ext == 'json' or ext == 'yaml', "Format Not Supported!"
        with open(config_path, 'r') as f:   
            if ext == 'yaml':
                data = yaml.safe_load(f)
            elif ext == 'json':
                data = json.load(f)
        for key,value in data.items():
            setattr(self,key,value)
        return data


simulation_config= CONFIG("Tools/simulation_config.yaml")
if __name__ == "__main__":
        device = torch.device('cpu')
        print("Using CPU")
else:
    system_config= CONFIG("Simulations/Particle_Tracking/config.yaml")
    if system_config.use_cuda:
        if torch.cuda.is_available():
            device = torch.device('cuda')
            torch.set_default_dtype(torch.float32) 
            torch.set_default_device('cuda')
        else:
            raise Exception("No GPU found, please set args.use_cuda = False")
    else:
        device = torch.device('cpu')
        torch.set_default_device('cpu')

# Physical quantities needed
Q_PROTON = torch.tensor(1.6022*1e-19)
MASS_PROTON_KG = torch.tensor(1.6726*1e-27)
MASS_PROTON_AMU = torch.tensor(1.0072766)
ATOMIC_NUMBER = 1
C = 3*1e8
DRIFT_VELOCITY_CM_US = 1 # [cm/us]

#Setup Chamber parameters
B = simulation_config.magnetic_field #Applied Magnetic Field (T)
E = torch.cos((Q_PROTON*B)/MASS_PROTON_KG) * simulation_config.electric_field #Applied Electric Field (V/m)
GAS_MEDIUM_DENSITY = simulation_config.gas_density #mg/cm3 at 1 bar

# Conversion macros
CM_NS__TO__M_S = 1e7
M_S__TO__CM_NS = 1e-7
CM__TO__M = 0.01
M_S_SQUARED__TO__CM_NS_SQUARED = 100 * (1e-9)**2

data = np.loadtxt(simulation_config.stopping_power_table_path,skiprows=1, dtype=float)
data = torch.tensor(data)
data = data.cpu().numpy()
energy_col = data[:, 0]  # First column [MeV]
stopping_power_col = data[:, 1]  # Second column [MeV / (mg/cm2)]

#Work around
torch.set_default_device('cpu')
ENERGY_TO_STOPPING_POWER_TABLE= splrep(energy_col, stopping_power_col)
torch.set_default_device(device.type)

class AtTpcMap:
    def __init__(self):
        self.fPadPlane = None
        self.kIsParsed = False
        self.fNumberPads = 10240
        self.AtPadCoord = np.zeros((self.fNumberPads, 4, 2), dtype=np.float32)
        self.bin_count = np.zeros(self.fNumberPads)

    def fill_coord(self, index, x, y, side, ort):
        # Left
        self.AtPadCoord[index][0][0] = x
        self.AtPadCoord[index][0][1] = y
        # Tip
        self.AtPadCoord[index][1][0] = x + side / 2
        self.AtPadCoord[index][1][1] = y + ort * side * np.sqrt(3) / 2
        # Right
        self.AtPadCoord[index][2][0] = x + side
        self.AtPadCoord[index][2][1] = y
        # Center 
        self.AtPadCoord[index][3][0],self.AtPadCoord[index][3][1] = self.orthocenter(self.AtPadCoord[index][0][0],self.AtPadCoord[index][0][1],
                                                                                     self.AtPadCoord[index][1][0],self.AtPadCoord[index][1][1],
                                                                                     self.AtPadCoord[index][2][0],self.AtPadCoord[index][2][1])
    def add_to_bin_count(self,X,Y,Z,energy_loss,x,y):
        # Flatten the meshgrid matrix
        mid_points_of_pad = self.AtPadCoord[:,-1,:]

        distances = np.abs(x - mid_points_of_pad[:,0]) + np.abs(y - mid_points_of_pad[:,1])
        relevant_pads = (distances < 1).nonzero()[0]

        distances = np.abs(X[..., np.newaxis]  - mid_points_of_pad[relevant_pads,0]) + np.abs(Y[..., np.newaxis] - mid_points_of_pad[relevant_pads,1])
        closest_index = relevant_pads[np.argmin(distances, axis=2)]
        aa = np.unique(closest_index)
        for id in aa :
            self.bin_count[id] += np.sum(-1 * energy_loss * Z[closest_index == id])


    def find_associated_pad(self,x,y):
        input_torch = False
        if torch.is_tensor(x):
            input_torch = True
            x = x.item()
            y = y.item()
        particle_xy_pos = np.array([x,y])
        mid_points_of_pad = self.AtPadCoord[:,-1,:]
        distances_from_pads = np.sum(np.abs(mid_points_of_pad - particle_xy_pos),axis=1)
        closest_pad_id = np.argmin(distances_from_pads)
        new_particle_xy_pos = mid_points_of_pad[closest_pad_id,:]
        new_x = torch.tensor(new_particle_xy_pos[0])
        new_y = torch.tensor(new_particle_xy_pos[1])

        if input_torch:
            return torch.tensor([new_x , new_y]).reshape(2,1)
        return new_x,new_y

    def GeneratePadPlane(self):

        small_z_spacing = simulation_config.small_z_spacing
        small_tri_side = simulation_config.small_tri_side
        umega_radius = simulation_config.umega_radius
        # beam_image_radius = 4842.52 * 2.54 / 1000. #Legacy

        small_x_spacing = 2. * small_z_spacing / np.sqrt(3.)
        small_y_spacing = small_x_spacing * np.sqrt(3.)
        dotted_s_tri_side = 4. * small_x_spacing + small_tri_side
        dotted_s_tri_hi = dotted_s_tri_side * np.sqrt(3.) / 2.
        dotted_l_tri_side = 2. * dotted_s_tri_side
        dotted_l_tri_hi = dotted_l_tri_side * np.sqrt(3.) / 2.
        large_x_spacing = small_x_spacing
        large_y_spacing = small_y_spacing
        large_tri_side = dotted_l_tri_side - 4. * large_x_spacing
        large_tri_hi = dotted_l_tri_side * np.sqrt(3.) / 2.

        # num_rows = 2 ** np.ceil(np.log(beam_image_radius / dotted_s_tri_side) / np.log(2.0)) #Legacy
        num_rows = np.floor(umega_radius / dotted_l_tri_hi)

        xoff = 0.
        yoff = 0.

        pad_index = 0
        for j in range(int(num_rows)):
            pads_in_half_hex = 0
            pads_in_hex = 0
            row_length = np.abs(np.sqrt(umega_radius**2 - (j * dotted_l_tri_hi + dotted_l_tri_hi / 2.)**2))

            #If row contains small pads
            if j < num_rows/2:
                pads_in_half_hex = (2 * num_rows - 2 * j) / 4.
                pads_in_hex = 2 * num_rows - 1. - 2. * j

            pads_in_half_row = row_length / dotted_l_tri_side
            pads_out_half_hex = int(np.round(2 * (pads_in_half_row - pads_in_half_hex)))
            pads_in_row = 2 * pads_out_half_hex + 4 * pads_in_half_hex - 1

            ort = 1
            for i in range(int(pads_in_row)):
                if i == 0:
                    if j % 2 == 0:
                        ort = -1
                    if ((pads_in_row - 1) / 2) % 2 == 1:
                        ort = -ort
                else:
                    ort = -ort

                pad_x_off = -(pads_in_half_hex + pads_out_half_hex / 2.) * dotted_l_tri_side + i * dotted_l_tri_side / 2. + 2. * large_x_spacing + xoff

                if i < pads_out_half_hex or i > (pads_in_hex + pads_out_half_hex - 1) or j > (num_rows / 2. - 1):
                    pad_y_off = j * dotted_l_tri_hi + large_y_spacing + yoff
                    if ort == -1:
                        pad_y_off += large_tri_hi

                    self.fill_coord(pad_index, pad_x_off, pad_y_off, large_tri_side, ort)
                    pad_index += 1
    
                    
                else:
                    pad_y_off = j * dotted_l_tri_hi + large_y_spacing + yoff
                    if ort == -1:
                        pad_y_off = j * dotted_l_tri_hi + 2 * dotted_s_tri_hi - small_y_spacing + yoff
                    self.fill_coord(pad_index, pad_x_off, pad_y_off, small_tri_side, ort)
                    pad_index += 1

                    tmp_pad_x_off = pad_x_off + dotted_s_tri_side / 2.
                    tmp_pad_y_off = pad_y_off + ort * dotted_s_tri_hi - 2 * ort * small_y_spacing
                    self.fill_coord(pad_index, tmp_pad_x_off, tmp_pad_y_off, small_tri_side, -ort)
                    pad_index += 1

                    tmp_pad_y_off = pad_y_off + ort * dotted_s_tri_hi
                    self.fill_coord(pad_index, tmp_pad_x_off, tmp_pad_y_off, small_tri_side, ort)
                    pad_index += 1

                    tmp_pad_x_off = pad_x_off + dotted_s_tri_side
                    self.fill_coord(pad_index, tmp_pad_x_off, pad_y_off, small_tri_side, ort)
                    pad_index += 1
        #mirror
        for i in range(pad_index):
            for j in range(4):
                self.AtPadCoord[i + pad_index][j][0] = self.AtPadCoord[i][j][0]
                self.AtPadCoord[i + pad_index][j][1] = -self.AtPadCoord[i][j][1]

    def draw_pads(self,show=True,plot_energy = False):
        fig, ax = plt.subplots()

        x = []
        y = []
        z = []
        for id,pad_coord in enumerate(self.AtPadCoord):
            pad_coords = [(x, y) for x, y in pad_coord[:3]]
            pad = Polygon(pad_coords, edgecolor='black', facecolor='none')
            ax.add_patch(pad)
            if self.bin_count[id] > 0:
                if self.bin_count[id]/max(self.bin_count) < 0.1:
                    continue
                x.append(pad_coord[-1][0])
                y.append(pad_coord[-1][1])
                z.append(self.bin_count[id])

        if len(z) > 0 and plot_energy:
            ax.scatter(x,y,c=z,cmap='viridis',s=5)
        ax.set_aspect('equal')
        ax.autoscale()
        if show:
            plt.show()
        return ax

    def orthocenter(self,x1, y1, x2, y2, x3, y3):
        # Function to find the equation of a line given two points
        def line_equation(point1, point2):
            x1, y1 = point1
            x2, y2 = point2
            slope = (y2 - y1) / (x2 - x1)
            intercept = y1 - slope * x1
            return slope, intercept

        # Calculate the equations of lines passing through each vertex and perpendicular to the opposite side
        slope_AB, intercept_AB = line_equation((x1, y1), (x3 + (x2-x3)/2, y3 + (y2-y3)/2))
        slope_BC, intercept_BC = line_equation((x3, y3), (x1 + (x2-x1)/2, y1 + (y2-y1)/2))

        # Function to find the intersection point of two lines
        def intersection_point(slope1, intercept1, slope2, intercept2):
            x = (intercept2 - intercept1) / (slope1 - slope2)
            y = slope1 * x + intercept1
            return x, y
        
        # The common intersection point is the orthocenter
        orthocenter = intersection_point(slope_AB, intercept_AB, slope_BC, intercept_BC)

        return orthocenter

class Trajectory():
    def __init__(self,traj_data,delta_t,data_source:Trajectory_Source = Trajectory_Source.Amit_Simulated,init_energy=None,init_teta=None,init_phi=None) -> None:
        self.data_src = data_source
        self.init_energy = init_energy
        self.init_teta = init_teta
        self.init_phi = init_phi
        self.t = traj_data['t'] if 't' in traj_data else torch.tensor([])
        self.delta_t = delta_t if delta_t is not None else 0.01
        self.real_energy = traj_data['energy'] if 'energy' in traj_data else torch.tensor([])
        self.generated_traj = traj_data['real_traj']
        self.x_real = traj_data['gt_traj']
        self.y = traj_data['obs_traj']
        self.traj_length = self.x_real.shape[1]

        self.x_estimated_FW = torch.zeros_like(self.x_real)
        self.x_estimated_BW = torch.zeros_like(self.x_real)

    def set_name(self,name):
        self.traj_name = name

    def traj_plots(self,SS_to_plot : List[Trajectory_SS_Type],show=True,plot_energy_on_pad = False):
        space_state_vector_list = []
        space_state_vector_list_velo = []
        energy_list = []
        color = []
        #Always make sure that observed is the last item in the list
        #this is done for naming reasons in plot
        if Trajectory_SS_Type.OT in SS_to_plot:
            index = SS_to_plot.index(Trajectory_SS_Type.OT)
            SS_to_plot.append(SS_to_plot.pop(index))
    
        for traj_ss_type in SS_to_plot:
            if traj_ss_type == Trajectory_SS_Type.GTT:
                space_state_vector_list.append(self.x_real)
                space_state_vector_list_velo.append(self.x_real)
                energy_list.append(self.real_energy)
                color.append('blue')
            elif traj_ss_type == Trajectory_SS_Type.FTT:
                space_state_vector_list.append(self.generated_traj)
                space_state_vector_list_velo.append(self.generated_traj)
                energy_list.append(self.real_energy)
                color.append('black')
            elif traj_ss_type == Trajectory_SS_Type.Estimated_FW:
                space_state_vector_list.append(self.x_estimated_FW)
                space_state_vector_list_velo.append(self.x_estimated_FW)
                energy_list.append(self.energy_estimated_FW)
                color.append('green')
            elif traj_ss_type == Trajectory_SS_Type.Estimated_BW:
                space_state_vector_list.append(self.x_estimated_BW)
                space_state_vector_list_velo.append(self.x_estimated_BW)
                energy_list.append(self.energy_estimated_BW)
            elif traj_ss_type == Trajectory_SS_Type.OT:
                space_state_vector_list.append(self.y)
                color.append('orange')


        fig = plt.figure()
        ax = fig.add_subplot(111, projection='3d')
        for i,space_state_vector in enumerate(space_state_vector_list):
            ax.scatter3D(space_state_vector[SS_VARIABLE.X.value,:], space_state_vector[SS_VARIABLE.Y.value,:], space_state_vector[SS_VARIABLE.Z.value,:],color=color[i],label=SS_to_plot[i].value,s=3)
        ax.legend()
        ax.set_xlabel('X')
        ax.set_ylabel('Y')
        ax.set_zlabel('Z')
        ax.set_title('3D Trajectory')

        fig = plt.figure()
        ax_pad = self.pad.draw_pads(show=False, plot_energy = plot_energy_on_pad)
        ax_no_pad = fig.add_subplot(111)
        for i,space_state_vector in enumerate(space_state_vector_list):
            ax_pad.scatter(space_state_vector[SS_VARIABLE.X.value,:], space_state_vector[SS_VARIABLE.Y.value,:],color=color[i],label=SS_to_plot[i].value,s=15)
            ax_no_pad.scatter(space_state_vector[SS_VARIABLE.X.value,:], space_state_vector[SS_VARIABLE.Y.value,:],color=color[i],label=SS_to_plot[i].value,s=3)
            if SS_to_plot[i] == Trajectory_SS_Type.OT:
                for i in range(len(space_state_vector[SS_VARIABLE.X.value,:])):
                    circle = plt.Circle((space_state_vector[SS_VARIABLE.X.value,i], space_state_vector[SS_VARIABLE.Y.value,i]), 1, color='r', fill=False)
                    plt.gca().add_artist(circle)
        ax_pad.legend()
        ax_pad.set_xlabel('X')
        ax_pad.set_ylabel('Y')
        ax_pad.set_title('2D Trajectory on Pad')
        ax_no_pad.legend()
        ax_no_pad.set_xlabel('X')
        ax_no_pad.set_ylabel('Y')
        ax_no_pad.set_title('2D Trajectory')

        # if len(space_state_vector_list_velo):
        #     fig, axs = plt.subplots(2)  # 2 rows of subplots
        #     for i,space_state_vector in enumerate(space_state_vector_list_velo):
        #         axs[0].scatter(range(len(space_state_vector[SS_VARIABLE.Vx.value,:])),space_state_vector[SS_VARIABLE.Vx.value,:],label=f'x {SS_to_plot[i].value}',s=3)
        #         axs[0].plot(space_state_vector[SS_VARIABLE.Vy.value,:],label=f'y {SS_to_plot[i].value}')
        #         axs[0].plot(space_state_vector[SS_VARIABLE.Vz.value,:],label=f'z {SS_to_plot[i].value}')
        #     axs[0].set_title("Velocities Over Time")
        #     axs[0].set_ylabel(f"Velocity [m/s]")
        #     axs[0].set_xticks([])
        #     axs[0].legend()
        #     for i,energy in enumerate(energy_list):
        #         axs[1].plot(self.t,energy.flatten(),label=f'Energy {SS_to_plot[i].value}')
        #     axs[1].set_title('Kinetic Energy Over Time')
        #     axs[1].set_xlabel('Time [s]')
        #     axs[1].set_ylabel('Energy [MeV]')
        #     axs[1].legend()
        if show:
            plt.show()

class Traj_Generator():
    def __init__(self,max_traj_length = simulation_config.max_traj_length) -> None:
        self.max_traj_length = max_traj_length
        self.real_traj = torch.zeros((6,self.max_traj_length ,1))
        self.obs_traj = torch.zeros((3,self.max_traj_length ,1))
        self.t = torch.zeros((self.max_traj_length ,1))
        self.energy = torch.zeros((self.max_traj_length ,1))
        self.delta_t = simulation_config.delta_t #step size in nseconds
        self.ATTPC_pad = AtTpcMap()
        self.ATTPC_pad.GeneratePadPlane()

    def gaussian_2d(self,mu, sigma):
        """
        DESCRIPTION:
            Calculate the value of 2D Gaussian distribution at point (x, y).
        INPUT:
            x (float): x-coordinate of the point.
            y (float): y-coordinate of the point.
            mu (tuple): Mean of the distribution in the form of (mean_x, mean_y).
            sigma (tuple): Standard deviation of the distribution in the form of (std_dev_x, std_dev_y).
        OUTPUT:
            float: Value of the 2D Gaussian distribution at point (x, y).
        """

        mean_x, mean_y = mu
        std_dev_x, std_dev_y = sigma
        x = np.linspace(mean_x-1, mean_x+1, 100)
        y = np.linspace(mean_y-1, mean_y+2, 100)
        X, Y = np.meshgrid(x, y)
        Z = (1 / (2 * np.pi * std_dev_x * std_dev_y) * 
                np.exp(-((X - mean_x)**2 / (2 * std_dev_x**2) + (Y - mean_y)**2 / (2 * std_dev_y**2))))
        return X,Y,Z
    def set_init_values(self,energy=None,theta=None,init_vx=None,init_vy=None,init_vz=None,phi=0,init_x=0,init_y=0,init_z=0):
        self.init_energy = energy
        self.init_teta = theta
        self.init_phi = phi
        self.real_traj[SS_VARIABLE.X.value,0,:]  = init_x
        self.real_traj[SS_VARIABLE.Y.value,0,:]  = init_y
        self.real_traj[SS_VARIABLE.Z.value,0,:]  = init_z
        if energy is None:
            self.real_traj[SS_VARIABLE.Vx.value,0,:] = init_vx
            self.real_traj[SS_VARIABLE.Vy.value,0,:] = init_vy
            self.real_traj[SS_VARIABLE.Vz.value,0,:] = init_vz
        else:
            M_Ener = MASS_PROTON_AMU * 931.49401
            # E = sqrt(p^2+ M_ener^2) - M_ener
            p = torch.sqrt((energy + M_Ener)**2 - M_Ener**2) # MeV/c
            v = convert_momentum_to_velocity(p)
            self.real_traj[SS_VARIABLE.Vx.value,0,:],self.real_traj[SS_VARIABLE.Vy.value,0,:],self.real_traj[SS_VARIABLE.Vz.value,0,:] = spherical_to_cartersian_co(mag=v,theta=theta,phi=phi)
            # self.real_traj[SS_VARIABLE.Vx.value,0,:] = v * np.sin(theta) * np.cos(phi)
            # self.real_traj[SS_VARIABLE.Vy.value,0,:] = v * np.sin(theta) * np.sin(phi)
            # self.real_traj[SS_VARIABLE.Vz.value,0,:] = v * np.cos(theta)
            # get_energy_from_velocities(v * np.sin(theta) * np.cos(phi),v * np.sin(theta) * np.sin(phi),v * np.cos(theta))
        self.obs_traj[:,0,:] =  self.real_traj[[SS_VARIABLE.X.value,SS_VARIABLE.Y.value,SS_VARIABLE.Z.value],0,:]

    def get_obs_traj_from_pad(self,traj_length):
        
        cluster_radius = 1 #cm
        observation_traj = torch.zeros((3,traj_length ,1))
        GT_traj = torch.zeros((6,traj_length ,1))
        index_of_GT = torch.zeros(traj_length,dtype=torch.int)

        real_traj_XY = self.obs_traj[[SS_VARIABLE.X.value,SS_VARIABLE.Y.value],:traj_length].squeeze().T #The XY of obs is still the real XY
        mid_points_of_pad = torch.tensor(self.ATTPC_pad.AtPadCoord[:,-1,:])


        # get point thats cluster_radius from first point
        distances = torch.sqrt(torch.sum(real_traj_XY**2,dim=1))
        distances-=cluster_radius
        distances = torch.abs(distances)

        cluster_mid_circle_point = real_traj_XY[torch.argmin(distances),:]

        i = 0
        while True:
            # calculated obs XY
            distance_central_circle_to_pads = torch.sum(torch.abs(cluster_mid_circle_point-mid_points_of_pad),dim=1)
            relevant_pads = (distance_central_circle_to_pads < cluster_radius).nonzero().reshape(-1)
            weights = self.ATTPC_pad.bin_count[relevant_pads] / np.sum(self.ATTPC_pad.bin_count[relevant_pads].squeeze())
            observation_traj[[SS_VARIABLE.X.value,SS_VARIABLE.Y.value],i] = torch.sum(mid_points_of_pad[relevant_pads] * weights.reshape(-1,1),axis=0).reshape(-1,1).float()
            # calculated obs Z
            distance_central_circle_to_real_XY_hits = torch.sqrt(torch.sum((real_traj_XY - cluster_mid_circle_point)**2,dim=1))
            id_real_traj_hits_in_circle = (distance_central_circle_to_real_XY_hits < cluster_radius).nonzero()
            observation_traj[SS_VARIABLE.Z.value,i] =torch.mean(self.obs_traj[SS_VARIABLE.Z.value,id_real_traj_hits_in_circle]) #TODO make it weighted

            #Get closest point of generated traj to observed for GT
            distance_real_traj_to_obs = torch.sum(torch.sqrt((self.real_traj[[SS_VARIABLE.X.value,SS_VARIABLE.Y.value,SS_VARIABLE.Z.value],:].squeeze() - observation_traj[:,i])**2),dim=0)
            id_of_closest_real_traj = torch.argmin(distance_real_traj_to_obs)
            GT_traj[:,i,:] = self.real_traj[:,id_of_closest_real_traj,:]
            index_of_GT[i] = id_of_closest_real_traj

            id_of_next_mid_circle_point = id_real_traj_hits_in_circle[-1] + 1
            if id_of_next_mid_circle_point > traj_length-1:
                break
            # get first point out of radius on the traj as next mid point
            cluster_mid_circle_point = real_traj_XY[id_of_next_mid_circle_point,:]
            i+=1

        return observation_traj[:,:i],GT_traj[:,:i],index_of_GT[:i]

    def generate(self,energy=None,theta=None,phi = 0,init_x = 0,init_y = 0,init_z = 0,init_vx=None,init_vy=None,init_vz=None):
        self.set_init_values(energy=energy,theta=theta,phi=phi,init_x=init_x,
                             init_y=init_y,init_z=init_z,init_vx=init_vx,
                             init_vy=init_vy,init_vz=init_vz)

        self.energy[0] = curr_energy = get_energy_from_velocities(self.real_traj[SS_VARIABLE.Vx.value,0,:],self.real_traj[SS_VARIABLE.Vy.value,0,:],self.real_traj[SS_VARIABLE.Vz.value,0,:])
        i=1
        z_resolution = DRIFT_VELOCITY_CM_US/simulation_config.sensor_sampling_rate_Mhz
        current_z_bucket = 0
        prev_z_bucket = 0
        idx_of_first_hit_in_current_bucket = 0

        off_pad_plane = False
        while (curr_energy > self.energy[0] * 0.01 and i<self.max_traj_length):
            state_space_vector_prev= self.real_traj[:,i-1,:].unsqueeze(0)
            curr_space_state_vector = f(state_space_vector_prev,self.delta_t,add_straggling=True)
            self.real_traj[:,i] = curr_space_state_vector

            self.t[i] = i * self.delta_t
            self.energy[i] = curr_energy = get_energy_from_velocities(self.real_traj[SS_VARIABLE.Vx.value,i],
                                                                      self.real_traj[SS_VARIABLE.Vy.value,i],
                                                                      self.real_traj[SS_VARIABLE.Vz.value,i])
            energy_loss = self.energy[i] - self.energy[i-1]


            #For XY, just copy from real trajectory. At the end this will be used for sensor granularity
            self.obs_traj[[SS_VARIABLE.X.value,SS_VARIABLE.Y.value],i] = self.real_traj[[SS_VARIABLE.X.value,SS_VARIABLE.Y.value],i]
            #For Z, use time buckets from sensory sampling
            current_z_bucket = self.real_traj[SS_VARIABLE.Z.value,i]//z_resolution
            if current_z_bucket!=prev_z_bucket:
                #linear interpolation inside bucket
                num_hits_in_z_bucket = i - idx_of_first_hit_in_current_bucket
                indicies_prev_bucket = torch.arange(num_hits_in_z_bucket)
                linear_interp = prev_z_bucket*z_resolution + indicies_prev_bucket * z_resolution/num_hits_in_z_bucket
                self.obs_traj[SS_VARIABLE.Z.value,idx_of_first_hit_in_current_bucket:i] = linear_interp.reshape(-1,1)
                #reset
                idx_of_first_hit_in_current_bucket = i
                prev_z_bucket = current_z_bucket

            closest_pad = self.ATTPC_pad.find_associated_pad(self.real_traj[SS_VARIABLE.X.value,i],self.real_traj[SS_VARIABLE.Y.value,i])
            distance_between_real_and_closest_pad = torch.sum(torch.abs(self.real_traj[[SS_VARIABLE.X.value,SS_VARIABLE.Y.value],i] -closest_pad))

            # criteria if to end trajectory early due to physical chamber constraints
            if distance_between_real_and_closest_pad >= 1 or self.real_traj[SS_VARIABLE.Z.value,i] > simulation_config.chamber_length:
                off_pad_plane = True
                break

            #Add to pad energy bins
            X,Y,Z = self.gaussian_2d((self.real_traj[SS_VARIABLE.X.value,i].item(),self.real_traj[SS_VARIABLE.Y.value,i].item()),(simulation_config.charge_std_x_axis,simulation_config.charge_std_y_axis))
            self.ATTPC_pad.add_to_bin_count(X.copy(),Y.copy(),Z,energy_loss.item(),self.real_traj[SS_VARIABLE.X.value,i].item(),self.real_traj[SS_VARIABLE.Y.value,i].item())
            i+=1

        # Z calculations for all the remaining hits
        num_hits_in_z_bucket = i - idx_of_first_hit_in_current_bucket
        indicies_prev_bucket = torch.arange(num_hits_in_z_bucket)
        linear_interp = prev_z_bucket*z_resolution + indicies_prev_bucket * z_resolution/num_hits_in_z_bucket
        self.obs_traj[SS_VARIABLE.Z.value,idx_of_first_hit_in_current_bucket:i] = linear_interp.reshape(-1,1)


        obs,gt,gt_idx = self.get_obs_traj_from_pad(i-1)
        traj_dict = {
            "t" : gt_idx,
            "real_traj" : self.real_traj[:i-1],
            "energy" : self.energy[:i-1],
            "gt_traj" : gt,
            "obs_traj" : obs,
        }
        traj = Trajectory(traj_data=traj_dict,init_energy=self.init_energy,init_teta=self.init_teta,init_phi=self.init_phi,delta_t=self.delta_t)
        traj.off_pad_plane = off_pad_plane #for debug purposes
        traj.pad = self.ATTPC_pad #for debug purposes

        _, estimated_para = get_mx_0(obs.squeeze(-1))
        return traj,estimated_para
    
def add_noise_to_list_of_trajectories(traj_list,mean=0,variance=0.1):
    for traj in traj_list:
        #zero mean 1 variance
        gaussian_noise_normal = torch.randn(traj.y.shape[0], traj.y.shape[1])
        gaussian_noise = mean + torch.sqrt(torch.tensor(variance)) * gaussian_noise_normal
        traj.y += gaussian_noise
    return traj_list

def error_estimations(type,real_energy,real_theta,real_phi,estimation):
    est_data = {
           f"MP phi {type}" : round((100*(estimation['initial_phi']-real_phi)/real_phi).item(),2),
           f"MP energy {type}" : round((100*(estimation['init_energy']-real_energy)/real_energy).item(),2),
           f"MP theta {type}" : round((100*(estimation['inital_theta']-real_theta)/real_theta).item(),2),
           f"SP energy {type}" : round((100*(estimation['inital_energy_point']-real_energy)/real_energy).item(),2) if type!="obs" else "-", #no SP energy estimation in OBS
           f"SP theta {type}" : round((100*(estimation['inital_theta_point']-real_theta)/real_theta).item(),2)
        }
    return est_data
def estimation_summary(traj_set,output_path,run_num):
    set_summary = list()
    for traj_id in range(len(traj_set)):

        first_cluster_energy = get_energy_from_velocities(traj_set[traj_id].x_real[3,0].squeeze(),traj_set[traj_id].x_real[4,0].squeeze(),traj_set[traj_id].x_real[5,0].squeeze())
        estimations ={
            "gen" : get_mx_0(traj_set[traj_id].generated_traj.squeeze(-1))[1],
            "obs" : get_mx_0(traj_set[traj_id].y.squeeze(-1),energy_at_first_cluster=first_cluster_energy,use_traj_for_energy=True)[1],
            "fw" :  get_mx_0(traj_set[traj_id].x_estimated_FW.squeeze(-1))[1],
            "bw" : get_mx_0(traj_set[traj_id].x_estimated_BW.squeeze(-1))[1],
            "real" : get_mx_0(traj_set[traj_id].x_real.squeeze(-1))[1]

        }
        real_energy = get_energy_from_velocities(traj_set[traj_id].x_real[3,0],traj_set[traj_id].x_real[4,0],traj_set[traj_id].x_real[5,0])#traj_set[traj_id].init_energy
        real_theta = traj_set[traj_id].init_teta
        real_phi = traj_set[traj_id].init_phi
        traj_data = [real_energy,real_theta]
        traj_data = {"ID" : traj_id,
                     "energy": real_energy.item(),
                     "theta" : real_theta,
                     "phi": real_phi}
        for type,est in estimations.items():
            traj_data = {**traj_data,**error_estimations(type,real_energy,real_theta,real_phi,est)}
            if type == 'gen':
                est_theta = torch.arctan2(torch.sqrt(traj_set[traj_id].generated_traj[0,1]**2 + traj_set[traj_id].generated_traj[1,1]**2),traj_set[traj_id].generated_traj[2,1])
                traj_data[f"SP theta {type}"] = round(torch.abs(100*(real_theta-est_theta)/real_theta).item(),2)
        if hasattr(traj_set[traj_id], 'BiRNN_output'):
            birnn_energy = get_energy_from_velocities(traj_set[traj_id].BiRNN_output[0],traj_set[traj_id].BiRNN_output[1],traj_set[traj_id].BiRNN_output[2])
            traj_data[f"BiRNN Est"] = round(torch.abs(100*(real_energy-birnn_energy)/real_energy).item(),2)
            traj_data["BiRNN Estimation"] = birnn_energy.item()
        set_summary.append(traj_data)

    df = pd.DataFrame(set_summary)
    df.set_index('ID', inplace=True)
    plt.figure()
    plt.scatter(df['energy'],df['MP energy obs'],s=2,label="MP Energy Obs")
    plt.scatter(df['energy'],df['SP energy bw'],s=2,label="SP Energy BW")
    plt.legend()
    plt.title(f"Energy Error Esimtation %\nAbs Avg {np.abs(df['SP energy bw']).mean()}, STD {np.std(df['SP energy bw'])}")
    plt.xlabel("Energy [MeV]")
    plt.ylabel("Error [%]")
    plt.grid()
    plt.savefig(os.path.join(output_path,f"Energy_Estimation_R{run_num}.png"))
    df.to_csv(os.path.join(output_path,f"Estimation_Summary_R{run_num}.csv"))

def spherical_to_cartersian_co(mag,theta,phi):
    x = mag * torch.sin(theta) * torch.cos(phi)
    y = mag * torch.sin(theta) * torch.sin(phi)
    z = mag * torch.cos(theta)
    return x , y , z

def get_energy_from_brho(brho):
    '''
    Input : 
        brho [Tm]
    Output : 
        energy - [MeV]
        p - [MeV/c]
    '''
    M_Ener = MASS_PROTON_AMU * 931.49401 #MeV
    p = brho * ATOMIC_NUMBER * (2.99792458 * 100) #MeV/c
    energy = torch.sqrt(p**2 + M_Ener**2) - M_Ener
    return energy,p

def plot_circle_with_fit(x_center_fit, y_center_fit, radius_fit,traj_x,traj_y):
    theta = np.linspace(0, 2*np.pi, 100)  # Create 100 points around the circumference
    x_fit = x_center_fit + radius_fit * np.cos(theta)  # Calculate x coordinates of points
    y_fit = y_center_fit + radius_fit * np.sin(theta)  # Calculate y coordinates of points
    plt.figure()
    plt.plot(x_fit,y_fit,label="fit",color='red')
    plt.scatter(traj_x,traj_y,label="true traj")
    plt.xlabel("X[cm]")
    plt.ylabel("Y[cm]")
    plt.legend()
    plt.show()

def get_mx_0(traj_coordinates,energy_at_first_cluster=None,error_perc=0,use_traj_for_energy=True):
    mx_0 = torch.zeros(6) #Size of state vector is 6x1

    # only use the beginning of the traj for estimation
    num_points_for_estimation = 30
    x = traj_coordinates[SS_VARIABLE.X.value,:num_points_for_estimation].cpu()
    y = traj_coordinates[SS_VARIABLE.Y.value,:num_points_for_estimation].cpu()
    z = traj_coordinates[SS_VARIABLE.Z.value,:num_points_for_estimation].cpu()
    model, inliers = ransac(torch.stack((x,y),dim=1).numpy(), CircleModel, min_samples=min(10,traj_coordinates.shape[1]),residual_threshold=6, max_trials=1000)
    x_center = model.params[0] 
    y_center = model.params[1] 
    init_radius = model.params[2] * CM__TO__M
    # plot_circle_with_fit(x_center * CM__TO__M,y_center * CM__TO__M,init_radius,x * CM__TO__M,y* CM__TO__M)

    y_from_center = y - y_center
    x_from_center = x - x_center

    phis = np.unwrap(torch.arctan2(y_from_center,x_from_center))
    phis = phis[0] - phis
    arc_lengths  = phis * init_radius

    ransacc = RANSACRegressor(LinearRegression(),min_samples=min(10,traj_coordinates.shape[1]),residual_threshold=6.0,max_trials=1000)
    ransacc.fit(arc_lengths.reshape(-1,1), z.numpy() * CM__TO__M)
    vector = torch.tensor([1,ransacc.estimator_.intercept_ + ransacc.estimator_.coef_[0]])
    vector /= torch.norm(vector,p=2)

    ## Init Angles ##
    init_theta = torch.arccos(vector[1])

    ### Init Energy ###
    if not(use_traj_for_energy) and energy_at_first_cluster is not None:
        init_energy = energy_at_first_cluster * (1 + error_perc/100)
        init_p = convert_velocity_to_momentum(get_velocity_from_energy(init_energy))
    else:
        brho = init_radius * B / torch.sin(init_theta)
        init_energy,init_p = get_energy_from_brho(brho)


    phis_over_time = torch.arctan2(y_from_center[1:]-y_from_center[0],x_from_center[1:]-x_from_center[0])
    avg_diff_between_phis = torch.median(phis_over_time.diff())
    init_phi = torch.median((phis_over_time - avg_diff_between_phis * torch.arange(len(phis_over_time),device='cpu'))[:50])

    estimated_parameters = {
        "inital_theta" : init_theta,
        "initial_phi" : init_phi,
        "init_radius" : init_radius,
        "init_energy" : init_energy,
        "inital_theta_point" : torch.arctan2(torch.sqrt(traj_coordinates[-3,0]**2 + traj_coordinates[-2,0]**2),traj_coordinates[-1,0]),
        "inital_energy_point" : get_energy_from_velocities(traj_coordinates[-3,0],traj_coordinates[-2,0],traj_coordinates[-1,0])

    }
    if simulation_config.mode == "generate_traj":
        print(estimated_parameters)
    mx_0[SS_VARIABLE.X.value] = x[0]
    mx_0[SS_VARIABLE.Y.value] = y[0]
    mx_0[SS_VARIABLE.Z.value] = z[0]
    # mx_0[SS_VARIABLE.Vx.value] = convert_momentum_to_velocity(init_p) * torch.sin(init_theta) * torch.cos(init_phi)
    # mx_0[SS_VARIABLE.Vy.value] = convert_momentum_to_velocity(init_p) * torch.sin(init_theta) * torch.sin(init_phi)
    # mx_0[SS_VARIABLE.Vz.value] = convert_momentum_to_velocity(init_p) * torch.cos(init_theta)
    mx_0[SS_VARIABLE.Vx.value],mx_0[SS_VARIABLE.Vy.value],mx_0[SS_VARIABLE.Vz.value] =  spherical_to_cartersian_co(mag=convert_momentum_to_velocity(init_p),theta=init_theta,phi=init_phi)
    return mx_0,estimated_parameters


    

def convert_momentum_to_velocity(p):
    '''
    Input : 
        p [MeV]
    Output : 
        v [cm/ns]
    '''
    v = (p * 5.344286e-22 / MASS_PROTON_KG) *  M_S__TO__CM_NS#cm/ns
    return v

def convert_velocity_to_momentum(v):
    '''
    Input : 
        v [cm/ns]
    Output : 
        p [MeV]
    '''
    p = (v * CM_NS__TO__M_S * MASS_PROTON_KG)/ 5.344286e-22 #MeV/c
    return p

def get_energy_from_velocities(vx,vy,vz):
    '''
    Input : 
        vx - [cm/ns]
        vy - [cm/ns]
        vz - [cm/ns]
    Output :
        energy - [MeV]
    '''
    M_Ener = MASS_PROTON_AMU * 931.49401
    v = torch.sqrt(vx**2 + vy**2 + vz**2)
    energy =  torch.sqrt(convert_velocity_to_momentum(v)**2 + M_Ener**2)-M_Ener

    return energy 

def get_velocity_from_energy(energy):
    '''
    Input : 
        energy - [MeV]
    Output :
        v - [cm/ns]
    '''
    M_Ener = MASS_PROTON_AMU * 931.49401
    # E = sqrt(p^2+ M_ener^2) - M_ener
    p = torch.sqrt((energy + M_Ener)**2 - M_Ener**2) # MeV/c
    v = convert_momentum_to_velocity(p)
    return v 

def get_vel_deriv(vx,vy,vz,direction,delta_t,add_energy_straggling=False):
    '''
    Input : 
        vx - [cm/ns]
        vy - [cm/ns]
        vz - [cm/ns]
        direction - 'x' / 'y' / 'z'
    Output :
        a - [cm/ns^2]
    '''
    energy = get_energy_from_velocities(vx,vy,vz) #MeV
    #convert velocities to m/s for computation
    temp_vx = vx * CM_NS__TO__M_S
    temp_vy = vy * CM_NS__TO__M_S
    temp_vz = vz * CM_NS__TO__M_S
    diff_pos = torch.sqrt((delta_t*vx)**2 + (delta_t*vy)**2 + (delta_t*vz)**2) #Rough estimate distance traveled with current velocities - needed for energy straggling
    deaccel = get_deacceleration(energy,add_energy_straggling,diff_pos)
    Bx = 0
    By = 0
    Bz = B
    Ex = 0
    Ey = 0
    Ez = -E

    rr = torch.sqrt(vx**2 + vy**2 + vz**2) + 1e-6
    az = torch.arctan2(vy,vx)
    po = torch.arccos(vz.clone()/rr.clone())
    if direction == 'x':
        a = (Q_PROTON/MASS_PROTON_KG) * (Ex + temp_vy*Bz-temp_vz*By) - deaccel*torch.sin(po)*torch.cos(az)
    elif direction == 'y':
        a = (Q_PROTON/MASS_PROTON_KG) * (Ey + temp_vz*Bx - temp_vx*Bz) - deaccel*torch.sin(po)*torch.sin(az)
    elif direction == 'z':
        a = (Q_PROTON/MASS_PROTON_KG) * (Ez + temp_vx*By - temp_vy*Bx) - deaccel*torch.cos(po)
    a = a * M_S_SQUARED__TO__CM_NS_SQUARED

    return a

def f(state_space_vector_prev,delta_t,add_straggling : bool = False):
    '''
    DESCRIPTION:
        RK4 Propagation
        In order to add energy straggling, we need to get the distance traveled with each f1/f2/f3, therefore we also 
        calculate RK1/RK2/Rk3 step to get a estimated distance traveled.
    INPUT:
        state_space_vector_prev - shape of [batch_size,space_vector_size]
        delta_t - RK step
    '''
    start = time.time()
    x = state_space_vector_prev[:,SS_VARIABLE.X.value]
    y = state_space_vector_prev[:,SS_VARIABLE.Y.value]
    z = state_space_vector_prev[:,SS_VARIABLE.Z.value]
    vx = state_space_vector_prev[:,SS_VARIABLE.Vx.value]
    vy = state_space_vector_prev[:,SS_VARIABLE.Vy.value]
    vz = state_space_vector_prev[:,SS_VARIABLE.Vz.value]

    ## f1 ##
    k1x = vx
    k1y = vy
    k1z = vz

    k1vx = get_vel_deriv(vx,vy,vz,direction='x',delta_t=delta_t,add_energy_straggling=add_straggling)
    k1vy = get_vel_deriv(vx,vy,vz,direction='y',delta_t=delta_t,add_energy_straggling=add_straggling)
    k1vz = get_vel_deriv(vx,vy,vz,direction='z',delta_t=delta_t,add_energy_straggling=add_straggling)

    ## f2 ##
    k2x = vx + 0.5 * delta_t * k1vx
    k2y = vy + 0.5 * delta_t * k1vy
    k2z = vz + 0.5 * delta_t * k1vz

    k2vx = get_vel_deriv(vx + 0.5*delta_t*k1vx,vy + 0.5*delta_t*k1vy,vz+ 0.5*delta_t*k1vz,direction='x',delta_t=delta_t,add_energy_straggling=add_straggling)
    k2vy = get_vel_deriv(vx + 0.5*delta_t*k1vx,vy + 0.5*delta_t*k1vy,vz+ 0.5*delta_t*k1vz,direction='y',delta_t=delta_t,add_energy_straggling=add_straggling)
    k2vz = get_vel_deriv(vx + 0.5*delta_t*k1vx,vy + 0.5*delta_t*k1vy,vz+ 0.5*delta_t*k1vz,direction='z',delta_t=delta_t,add_energy_straggling=add_straggling)

    ## f3 ##
    k3x = vx + 0.5 * delta_t * k2vx
    k3y = vy + 0.5 * delta_t * k2vy
    k3z = vz + 0.5 * delta_t * k2vz

    k3vx = get_vel_deriv(vx + 0.5*delta_t*k2vx,vy + 0.5*delta_t*k2vy,vz+ 0.5*delta_t*k2vz,direction='x',delta_t=delta_t,add_energy_straggling=add_straggling)
    k3vy = get_vel_deriv(vx + 0.5*delta_t*k2vx,vy + 0.5*delta_t*k2vy,vz+ 0.5*delta_t*k2vz,direction='y',delta_t=delta_t,add_energy_straggling=add_straggling)
    k3vz = get_vel_deriv(vx + 0.5*delta_t*k2vx,vy + 0.5*delta_t*k2vy,vz+ 0.5*delta_t*k2vz,direction='z',delta_t=delta_t,add_energy_straggling=add_straggling)

    ## f4 ##
    k4x = vx + delta_t * k3vx
    k4y = vy + delta_t * k3vy
    k4z = vz + delta_t * k3vz

    k4vx = get_vel_deriv(vx + delta_t*k3vx,vy + delta_t*k3vy,vz+ delta_t*k3vz,direction='x',delta_t=delta_t,add_energy_straggling=add_straggling)
    k4vy = get_vel_deriv(vx + delta_t*k3vx,vy + delta_t*k3vy,vz+ delta_t*k3vz,direction='y',delta_t=delta_t,add_energy_straggling=add_straggling)
    k4vz = get_vel_deriv(vx + delta_t*k3vx,vy + delta_t*k3vy,vz+ delta_t*k3vz,direction='z',delta_t=delta_t,add_energy_straggling=add_straggling)


    ##RK Final Step 
    delta_vx = (delta_t/6) * (k1vx+ 2*k2vx + 2*k3vx + k4vx)
    delta_vy = (delta_t/6) * (k1vy + 2*k2vy + 2*k3vy + k4vy)
    delta_vz = (delta_t/6) * (k1vz + 2*k2vz + 2*k3vz + k4vz)

    vx = vx + delta_vx
    vy = vy + delta_vy
    vz = vz + delta_vz

    d4_x = (delta_t/6) * (k1x + 2*k2x + 2*k3x + k4x)
    d4_y = (delta_t/6) * (k1y + 2*k2y + 2*k3y + k4y)
    d4_z = (delta_t/6) * (k1z + 2*k2z + 2*k3z + k4z)
    RK4_diff_pos = torch.sqrt(d4_x**2 + d4_y**2 + d4_z**2)

    x = x + d4_x
    y = y + d4_y
    z = z + d4_z

    if add_straggling: #only used in generation not in KF
        x,y,z = add_angular_straggling(x,y,z,get_energy_from_velocities(vx,vy,vz),RK4_diff_pos)


    state_space_vector_curr = torch.cat((x.reshape(-1,1),y.reshape(-1,1),z.reshape(-1,1),vx.reshape(-1,1),vy.reshape(-1,1),vz.reshape(-1,1)),dim=1).unsqueeze(-1)
    return state_space_vector_curr

def h(space_state_vector):
    ''' 
    INPUT:
        space_state_vector - shape of [batch_size,space_state_vector_size,1]
    OUTPUT:
        space_state_vector - shape of [batch_size,observation_vector_size,1]

    '''
    H = torch.zeros(3,space_state_vector.shape[1])
    H[0,0] = H[1,1] = H[2,2] = 1
    obs_vector = torch.matmul(H,space_state_vector)
    return obs_vector

def get_energy_straggling(delta_energy):

    # Calculate Energy Straggling
    c_factor = 14 * torch.sqrt(torch.tensor(0.5)) #14 * sqrt((Z_p * Z_t) / (Z_p**-0.33 + Z_t**-0.33)) ---> Atomic # is 1
    c_factor *= 1.65 * 2.35 #multiply by factor as in paper
    energy_straggling_FWHM = delta_energy**0.53 * c_factor #FWHM
    energy_straggling_std = energy_straggling_FWHM/(2*(2*torch.log(torch.tensor(2.0)))) #Gaussian: FWHM = 2 * sqrt(2ln2) * std
    energy_straggling = torch.randn(1) * energy_straggling_std

    return energy_straggling * 1e-3 #convert from KeV to MeV

def add_angular_straggling(x,y,z,energy,dist_traveled):

    #Calculate Angular Straggling
    tau = 41.5e3 * GAS_MEDIUM_DENSITY * dist_traveled / (2) #M2 = Z1 = Z2 = 1 --> (M2(Z1**2/3 + Z2**2/3)) = 1
    alpha_tag = 1*tau ** 0.55 if tau > 1000 else 0.92*tau**0.56
    angular_straggling_FWHM = alpha_tag * torch.sqrt(torch.tensor(2.0)) / (16.26 * energy)# Z1Z2 * sqrt(Z1**2/3 + Z2**2/3) = sqrt(2)
    angular_straggling_std = angular_straggling_FWHM/(2*torch.sqrt(2*torch.log(torch.tensor(2.0)))) #Gaussian: FWHM = 2 * sqrt(2ln2) * std
    angular_straggling_milirads = torch.randn(1) * angular_straggling_std
    angular_straggling = angular_straggling_milirads * 1e-3 #convert from mili rads to rads


    #Convert to spherical coordinates and add angular straggling
    radius = torch.sqrt(torch.pow(x,2) + torch.pow(y,2) + torch.pow(z,2))
    phi = torch.arctan2(y,x)
    theta = torch.arccos(z/radius)

    new_theta = theta + angular_straggling
    new_phi = phi + angular_straggling

    #Convert back to cartesian coordinates
    new_x = radius * torch.sin(new_theta) * torch.cos(new_phi)
    new_y = radius * torch.sin(new_theta) * torch.sin(new_phi)
    new_z = radius * torch.cos(new_theta)

    return new_x,new_y,new_z

def get_deacceleration(energy_interp,add_energy_straggling,delta_pos):
    '''
    Input : 
        energy_interp - [MeV]
    Output :
        interp_stopping_acc - [m/s^2]
    '''

    # #Work around
    torch.set_default_device('cpu')
    interp_stopping_power = splev(energy_interp.cpu().detach(), ENERGY_TO_STOPPING_POWER_TABLE)    #MeV/(mg/cm2)
    torch.set_default_device(device.type)  
    if device.type == 'cuda':
        interp_stopping_power = torch.from_numpy(interp_stopping_power).cuda()

    if add_energy_straggling:
        energy_loss = torch.tensor(interp_stopping_power) * GAS_MEDIUM_DENSITY * delta_pos
        energy_straggling = get_energy_straggling(energy_loss)
        interp_stopping_power = (energy_straggling + energy_loss) / (GAS_MEDIUM_DENSITY * delta_pos)

    interp_stopping_force =interp_stopping_power * 1.6021773349e-13 * GAS_MEDIUM_DENSITY / CM__TO__M #MeV/m
    interp_stopping_acc = interp_stopping_force / MASS_PROTON_KG
    return interp_stopping_acc.float()

def generate_dataset(N_Train,N_Test,N_CV,dataset_name ,output_dir):
    time_stamp = datetime.now().strftime("_%d_%m_%y__%H_%M")
    dataset_name = dataset_name + time_stamp
    os.makedirs(output_dir,exist_ok=True)
    assert dataset_name not in os.listdir(output_dir), f"Dataset with name '{dataset_name}' Exists!"

    ## Get Angle-Energy
    data = np.loadtxt(simulation_config.angle_energy_table_path)
    relevant_indices = np.arange(0,data.shape[0],simulation_config.sub_sample_rate_data)
    data = data[relevant_indices]

    theta = np.radians(data[:, 0]) 
    energy = data[:, 1]  #MeV
    np.random.shuffle(permutation :=np.arange(1, len(energy)))
    theta = theta[permutation].reshape(-1)
    energy = energy[permutation].reshape(-1)
    phi = np.random.uniform(-np.pi, np.pi,len(theta))

    generator = Traj_Generator()
    training_set = list()
    CV_set = list()
    test_set = list()
    traj_meta_data = {"ID": [],"energy":[],"est_energy":[],'energy_error_%':[],"est_energy_point":[],'energy_point_error_%':[],"phi":[],"est_phi":[],'phi_error_%':[],"theta":[],"est_theta":[],'theta_error_%':[],"est_theta_point":[],'theta_point_error_%':[],'set':[]}
    for i in range(N_Train + N_CV + N_Test):
        print(f"Generating trajectory {i}; Energy : {energy[i]},Theta : {theta[i]},Phi : {phi[i]}")
        traj,obs_traj_estimated_parameters = generator.generate(energy=energy[i],theta=theta[i],phi=phi[i])

        traj.ID = i
        traj_meta_data['ID'].append(i)
        traj_meta_data['energy'].append(energy[i])
        traj_meta_data['est_energy'].append(obs_traj_estimated_parameters['init_energy'].item())
        traj_meta_data['energy_error_%'].append(np.abs(100 * (obs_traj_estimated_parameters['init_energy'].item() - energy[i])/energy[i]))
        traj_meta_data['est_energy_point'].append(obs_traj_estimated_parameters['inital_energy_point'].item())
        traj_meta_data['energy_point_error_%'].append(np.abs(100 * (obs_traj_estimated_parameters['inital_energy_point'].item() - energy[i])/energy[i]))
        traj_meta_data['phi'].append(phi[i])
        traj_meta_data['est_phi'].append(obs_traj_estimated_parameters['initial_phi'].item())
        traj_meta_data['phi_error_%'].append(np.abs(100 * (obs_traj_estimated_parameters['initial_phi'].item() - phi[i])/phi[i]))
        traj_meta_data['theta'].append(theta[i])
        traj_meta_data['est_theta'].append(obs_traj_estimated_parameters['inital_theta'].item())
        traj_meta_data['theta_error_%'].append(np.abs(100 * (obs_traj_estimated_parameters['inital_theta'].item() - theta[i])/theta[i]))
        traj_meta_data['est_theta_point'].append(obs_traj_estimated_parameters['inital_theta_point'].item())
        traj_meta_data['theta_point_error_%'].append(np.abs(100 * (obs_traj_estimated_parameters['inital_theta_point'].item() - theta[i])/theta[i]))

        if i<N_Train:
            traj_meta_data['set'].append('train')
            training_set.append(copy.deepcopy(traj))
        elif i < N_CV + N_Train:
            traj_meta_data['set'].append('validation')
            CV_set.append(copy.deepcopy(traj))
        else:
            traj_meta_data['set'].append('test')
            test_set.append(copy.deepcopy(traj))

    traj_meta_data_df = pd.DataFrame(traj_meta_data)
    traj_meta_data_df.set_index('ID', inplace=True)
    traj_meta_data_df.to_csv(os.path.join(output_dir,f'{dataset_name}_traj_metadata.csv'))

    torch.save([training_set,CV_set,test_set], os.path.join(output_dir,f'{dataset_name}.pt'))

def setup_logger(log_file):

    # If the log file already exists, delete it
    if os.path.exists(log_file):
        os.remove(log_file)

    # Create a custom logger
    logger = logging.getLogger('file_copy_logger')
    logger.setLevel(logging.DEBUG)  # Set the logging level

    # Create handlers
    file_handler = logging.FileHandler(log_file)
    console_handler = logging.StreamHandler()

    # Set the logging level for each handler
    file_handler.setLevel(logging.DEBUG)
    console_handler.setLevel(logging.DEBUG)

    # Add the handlers to the logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def setup_table_formats(input_file):

    # Load the data from the text file
    df = pd.read_csv(input_file, delim_whitespace=True, header=None)

    def convert_to_float(value):
        return float(value.replace(',', '|').replace('.', ',').replace('|', '.'))

    # Function to convert energy values to MeV
    def convert_to_mev(energy, unit):
        energy = energy.replace(',', '|').replace('.', ',').replace('|', '.')
        if unit == 'eV':
            return str(float(energy) / 1000000)
        elif unit == 'keV':
            return str(float(energy) / 1000)
        elif unit == 'MeV':  # Already in MeV
            return str(energy)
        else:
            assert False, f"Unknown energy unit : {unit}"

    # Function to convert length values to um
    def convert_to_cm(value, unit):
        value = float(value.replace(',', '|').replace('.', ',').replace('|', '.'))
        if unit == 'um':
            return value / 10000 # 1 um = 0.0001 cm
        elif unit == 'mm':
            return value / 10    # 1 mm = 0.1 cm
        elif unit == 'cm':
            return value         # If already in cm, return as is
        elif unit == 'm':
            return value * 100   # If already in cm, return as is
        elif unit == 'km':
            return value * 100000        
        else:
            assert False, f"Unknown length unit : {unit}"

    # Apply conversions
    df[0] = df.apply(lambda row: convert_to_mev(row[0], row[1]), axis=1)
    df[4] = df.apply(lambda row: convert_to_cm(row[4], row[5]), axis=1)
    df[6] = df.apply(lambda row: convert_to_cm(row[6], row[7]), axis=1)
    df[8] = df.apply(lambda row: convert_to_cm(row[8], row[9]), axis=1)

    #sum power loss
    df[2] = df[2].apply(convert_to_float)
    df[3] = df[3].apply(convert_to_float)
    df[2] = df[2] + df[3]

    df = df.iloc[:, [0, 2, 4, 6, 8]]

    # Write the modified DataFrame to a new text file without units
    headers = ['Energy [MeV]','dE/dx','Range [cm]','Longitudinal Straggling [cm]','Lateral Straggling [cm]']
    df.to_csv(os.path.join("Tools","stpHydrogen_new.txt"), sep=' ', index=False, header=headers)

if __name__ == "__main__":
    if simulation_config.mode == "generate_dataset":
        generate_dataset(N_Train=simulation_config.num_train_traj,
                         N_CV=simulation_config.num_val_traj,
                         N_Test=simulation_config.num_test_traj,
                         dataset_name=simulation_config.dataset_name,
                         output_dir=simulation_config.output_dir)
    
    if simulation_config.mode == "generate_traj":
        gen = Traj_Generator()
        traj,_ = gen.generate(energy=simulation_config.energy,
                              theta=simulation_config.theta,
                              phi=simulation_config.phi)

        traj_to_plot = []
        if simulation_config.plot_real_traj:
            traj_to_plot.append(Trajectory_SS_Type.GTT)
        if simulation_config.plot_observed_traj:
            traj_to_plot.append(Trajectory_SS_Type.OT)

        if simulation_config.plot_traj and len(traj_to_plot):
            traj.traj_plots(traj_to_plot,plot_energy_on_pad=simulation_config.plot_energy_on_pad)
        # df = pd.DataFrame(traj.y.squeeze(-1).numpy().T,columns = ['x','y','z','vx','vy','vz'])
        # df.to_csv('debug_traj_energy_2_teta_1_phi_0.csv', index=False)




